# SPDX-License-Identifier: LGPL-2.1-or-later

import asyncio
import base64
import contextlib
import dataclasses
import enum
import errno
import fcntl
import hashlib
import json
import logging
import os
import random
import resource
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Optional

from mkosi.config import (
    Args,
    Config,
    ConfigFeature,
    Network,
    OutputFormat,
    QemuDrive,
    QemuFirmware,
    QemuVsockCID,
    finalize_term,
    format_bytes,
    systemd_tool_version,
    want_selinux_relabel,
    yes_no,
)
from mkosi.log import ARG_DEBUG, die
from mkosi.partition import finalize_root, find_partitions
from mkosi.run import SD_LISTEN_FDS_START, AsyncioThread, find_binary, fork_and_wait, run, spawn, workdir
from mkosi.tree import copy_tree, rmtree
from mkosi.types import PathString
from mkosi.user import INVOKING_USER, become_root_in_subuid_range, become_root_in_subuid_range_cmd
from mkosi.util import StrEnum, current_home_dir, flock, flock_or_die, groupby, round_up, try_or
from mkosi.versioncomp import GenericVersion

QEMU_KVM_DEVICE_VERSION = GenericVersion("9.0")
VHOST_VSOCK_SET_GUEST_CID = 0x4008AF60


class QemuDeviceNode(StrEnum):
    kvm = enum.auto()
    vhost_vsock = enum.auto()

    def device(self) -> Path:
        return Path("/dev") / str(self)

    def description(self) -> str:
        return {
            QemuDeviceNode.kvm: "KVM acceleration",
            QemuDeviceNode.vhost_vsock: "a VSock device",
        }[self]

    def feature(self, config: Config) -> ConfigFeature:
        return {
            QemuDeviceNode.kvm: config.qemu_kvm,
            QemuDeviceNode.vhost_vsock: config.qemu_vsock,
        }[self]

    def open(self) -> int:
        return os.open(self.device(), os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK)

    def available(self, log: bool = False) -> bool:
        try:
            os.close(self.open())
        except OSError as e:
            if e.errno not in (errno.ENOENT, errno.ENODEV, errno.EPERM, errno.EACCES):
                raise e

            if log and e.errno in (errno.ENOENT, errno.ENODEV):
                logging.warning(
                    f"{self.device()} not found. Not adding {self.description()} to the virtual machine."
                )

            if log and e.errno in (errno.EPERM, errno.EACCES):
                logging.warning(
                    f"Permission denied to access {self.device()}. "
                    f"Not adding {self.description()} to the virtual machine. "
                    "(Maybe a kernel module could not be loaded?)"
                )

            return False

        return True


def hash_output(config: Config) -> "hashlib._Hash":
    p = os.fspath(config.output_dir_or_cwd() / config.output)
    return hashlib.sha256(p.encode())


def hash_to_vsock_cid(hash: "hashlib._Hash") -> int:
    cid = int.from_bytes(hash.digest()[:4], byteorder="little")
    # Make sure we don't return any of the well-known CIDs.
    return max(3, min(cid, 0xFFFFFFFF - 1))


def vsock_cid_in_use(vfd: int, cid: int) -> bool:
    try:
        fcntl.ioctl(vfd, VHOST_VSOCK_SET_GUEST_CID, struct.pack("=Q", cid))
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise

        return True

    return False


def find_unused_vsock_cid(config: Config, vfd: int) -> int:
    hash = hash_output(config)

    for i in range(64):
        cid = hash_to_vsock_cid(hash)

        if not vsock_cid_in_use(vfd, cid):
            return cid

        hash.update(i.to_bytes(length=4, byteorder="little"))

    for i in range(64):
        cid = random.randint(0, 0xFFFFFFFF - 1)

        if not vsock_cid_in_use(vfd, cid):
            return cid

    die("Failed to find an unused VSock connection ID")


class KernelType(StrEnum):
    pe = enum.auto()
    uki = enum.auto()
    unknown = enum.auto()

    @classmethod
    def identify(cls, config: Config, path: Path) -> "KernelType":
        if not config.find_binary("bootctl"):
            logging.warning("bootctl is not installed, assuming 'unknown' kernel type")
            return KernelType.unknown

        if (v := systemd_tool_version("bootctl", sandbox=config.sandbox)) < 253:
            logging.warning(f"bootctl {v} doesn't know kernel-identify verb, assuming 'unknown' kernel type")
            return KernelType.unknown

        type = run(
            ["bootctl", "kernel-identify", workdir(path)],
            stdout=subprocess.PIPE,
            sandbox=config.sandbox(binary="bootctl", options=["--ro-bind", path, workdir(path)]),
        ).stdout.strip()

        try:
            return cls(type)
        except ValueError:
            logging.warning(f"Unknown kernel type '{type}', assuming 'unknown'")
            return KernelType.unknown


@dataclasses.dataclass(frozen=True)
class OvmfConfig:
    description: Path
    firmware: Path
    format: str
    vars: Path
    vars_format: str


def find_ovmf_firmware(config: Config, qemu: Path, firmware: QemuFirmware) -> Optional[OvmfConfig]:
    if not firmware.is_uefi():
        return None

    tools = Path("/") if any(qemu.is_relative_to(d) for d in config.extra_search_paths) else config.tools()

    desc = list((tools / "usr/share/qemu/firmware").glob("*"))
    if tools == Path("/"):
        desc += list((tools / "etc/qemu/firmware").glob("*"))

    arch = config.architecture.to_qemu()
    machine = config.architecture.default_qemu_machine()

    for p in sorted(desc):
        if p.is_dir():
            continue

        j = json.loads(p.read_text())

        if "uefi" not in j["interface-types"]:
            logging.debug(f"{p.name} firmware description does not target UEFI, skipping")
            continue

        for target in j["targets"]:
            if target["architecture"] != arch:
                continue

            # We cannot use fnmatch as for example our default machine for x86-64 is q35 and the firmware
            # description lists "pc-q35-*" so we use a substring check instead.
            if any(machine in glob for glob in target["machines"]):
                break
        else:
            logging.debug(
                f"{p.name} firmware description does not target architecture {arch} or "
                f"machine {machine}, skipping"
            )
            continue

        if firmware == QemuFirmware.uefi_secure_boot and "secure-boot" not in j["features"]:
            logging.debug(f"{p.name} firmware description does not include secure boot, skipping")
            continue

        if firmware != QemuFirmware.uefi_secure_boot and "secure-boot" in j["features"]:
            logging.debug(f"{p.name} firmware description includes secure boot, skipping")
            continue

        if config.qemu_firmware_variables == Path("microsoft") and "enrolled-keys" not in j["features"]:
            logging.debug(f"{p.name} firmware description does not have enrolled Microsoft keys, skipping")
            continue

        if config.qemu_firmware_variables != Path("microsoft") and "enrolled-keys" in j["features"]:
            logging.debug(f"{p.name} firmware description has enrolled Microsoft keys, skipping")
            continue

        logging.debug(f"Using {p.name} firmware description")

        return OvmfConfig(
            description=Path("/") / p.relative_to(tools),
            firmware=Path(j["mapping"]["executable"]["filename"]),
            format=j["mapping"]["executable"]["format"],
            vars=Path(j["mapping"]["nvram-template"]["filename"]),
            vars_format=j["mapping"]["nvram-template"]["format"],
        )

    die("Couldn't find matching OVMF UEFI firmware description")


@contextlib.contextmanager
def start_swtpm(config: Config) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="mkosi-swtpm-") as state:
        # swtpm_setup is noisy and doesn't have a --quiet option so we pipe it's stdout to /dev/null.
        run(
            [
                "swtpm_setup",
                "--tpm-state", workdir(Path(state)),
                "--tpm2",
                "--pcr-banks",
                "sha256",
                "--config", "/dev/null",
            ],
            sandbox=config.sandbox(
                binary="swtpm_setup",
                options=["--bind", state, workdir(Path(state))],
            ),
            stdout=None if ARG_DEBUG.get() else subprocess.DEVNULL,
        )  # fmt: skip

        cmdline = ["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={workdir(Path(state))}"]

        # We create the socket ourselves and pass the fd to swtpm to avoid race conditions where we start
        # qemu before swtpm has had the chance to create the socket (or where we try to chown it first).
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            path = Path(state) / Path("sock")
            sock.bind(os.fspath(path))
            sock.listen()

            cmdline += ["--ctrl", f"type=unixio,fd={SD_LISTEN_FDS_START}"]

            with spawn(
                cmdline,
                pass_fds=(sock.fileno(),),
                sandbox=config.sandbox(
                    binary="swtpm",
                    options=["--bind", state, workdir(Path(state))],
                    setup=scope_cmd(
                        name=f"mkosi-swtpm-{config.machine_or_name()}",
                        description=f"swtpm for {config.machine_or_name()}",
                    ),
                ),
            ) as proc:
                yield path
                proc.terminate()


def find_virtiofsd(*, root: Path = Path("/"), extra: Sequence[Path] = ()) -> Optional[Path]:
    if p := find_binary("virtiofsd", root=root, extra=extra):
        return p

    if (p := root / "usr/libexec/virtiofsd").exists():
        return Path("/") / p.relative_to(root)

    if (p := root / "usr/lib/virtiofsd").exists():
        return Path("/") / p.relative_to(root)

    return None


def unshare_version() -> str:
    return run(["unshare", "--version"], stdout=subprocess.PIPE).stdout.strip().split()[-1]


def systemd_escape(config: Config, s: PathString, path: bool = False) -> str:
    cmdline = ["systemd-escape", s]
    if path:
        cmdline += ["--path"]

    return run(
        cmdline, stdout=subprocess.PIPE, sandbox=config.sandbox(binary="systemd-escape")
    ).stdout.strip()


@contextlib.contextmanager
def start_virtiofsd(
    config: Config,
    directory: Path,
    *,
    uidmap: bool = True,
    name: Optional[str] = None,
    selinux: bool = False,
) -> Iterator[Path]:
    if name is None:
        name = systemd_escape(config, directory, path=True)

    virtiofsd = find_virtiofsd(root=config.tools(), extra=config.extra_search_paths)
    if virtiofsd is None:
        die("virtiofsd must be installed to boot directory images or use RuntimeTrees= with mkosi qemu")

    cmdline: list[PathString] = [
        virtiofsd,
        "--shared-dir", workdir(directory),
        "--xattr",
        # qemu's client doesn't seem to support announcing submounts so disable the feature to avoid the
        # warning.
        "--no-announce-submounts",
        "--sandbox=chroot",
        f"--inode-file-handles={'prefer' if os.getuid() == 0 and not uidmap else 'never'}",
    ]  # fmt: skip

    if selinux:
        cmdline += ["--security-label"]

    st = None
    if uidmap:
        st = Path(directory).stat()

        # If we're already running as the same user that we'll be running virtiofsd as, don't bother doing
        # any explicit user switching or chown()'ing as it's not needed in this case.
        if st.st_uid == os.getuid() and st.st_gid == os.getgid():
            st = None

    # We create the socket ourselves and pass the fd to virtiofsd to avoid race conditions where we start
    # qemu before virtiofsd has had the chance to create the socket (or where we try to chown it first).
    with (
        tempfile.TemporaryDirectory(prefix="mkosi-virtiofsd-") as context,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock,
    ):
        if st:
            # Make sure virtiofsd can access the socket in this directory.
            os.chown(context, st.st_uid, st.st_gid)

        # Make sure we can use the socket name as a unique identifier for the fs as well but make sure it's
        # not too long as virtiofs tag names are limited to 36 bytes.
        path = Path(context) / f"sock-{uuid.uuid4().hex}"[:35]
        sock.bind(os.fspath(path))
        sock.listen()

        if st:
            # Make sure virtiofsd can connect to the socket.
            os.chown(path, st.st_uid, st.st_gid)

        cmdline += ["--fd", str(SD_LISTEN_FDS_START)]

        # We want RuntimeBuildSources= and RuntimeTrees= to do the right thing even when running mkosi qemu
        # as root without the source directories necessarily being owned by root. We achieve this by running
        # virtiofsd as the owner of the source directory and then mapping that uid to root.

        name = f"mkosi-virtiofsd-{name}"
        description = f"virtiofsd for {directory}"
        scope = []
        if st:
            scope = scope_cmd(name=name, description=description, user=st.st_uid, group=st.st_gid)
        elif not uidmap and (os.getuid() == 0 or unshare_version() >= "2.38"):
            scope = scope_cmd(name=name, description=description)

        with spawn(
            cmdline,
            pass_fds=(sock.fileno(),),
            user=st.st_uid if st and not scope else None,
            group=st.st_gid if st and not scope else None,
            # If we're booting from virtiofs and unshare is too old, we don't set up a scope so we can use
            # our own function to become root in the subuid range.
            # TODO: Drop this as soon as we drop CentOS Stream 9 support and can rely on newer unshare
            # features.
            preexec_fn=become_root_in_subuid_range if not scope and not uidmap else None,
            sandbox=config.sandbox(
                binary=virtiofsd,
                options=[
                    "--bind", directory, workdir(directory),
                    *(["--become-root"] if uidmap else []),
                ],
                setup=scope + become_root_in_subuid_range_cmd() if scope and not uidmap else [],
            ),
        ) as proc:  # fmt: skip
            yield path
            proc.terminate()


@contextlib.contextmanager
def vsock_notify_handler() -> Iterator[tuple[str, dict[str, str]]]:
    """
    This yields a vsock address and a dict that will be filled in with the notifications from the VM. The
    dict should only be accessed after the context manager has been finalized.
    """
    with socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as vsock:
        vsock.bind((socket.VMADDR_CID_ANY, socket.VMADDR_PORT_ANY))
        vsock.listen()
        vsock.setblocking(False)

        num_messages = 0
        num_bytes = 0
        messages = {}

        async def notify() -> None:
            nonlocal num_messages
            nonlocal num_bytes
            loop = asyncio.get_running_loop()

            while True:
                s, _ = await loop.sock_accept(vsock)

                num_messages += 1

                with s:
                    data = []
                    try:
                        while buf := await loop.sock_recv(s, 4096):
                            data.append(buf)
                    except ConnectionResetError:
                        logging.debug("vsock notify listener connection reset by peer")

                for msg in b"".join(data).decode().split("\n"):
                    if not msg:
                        continue

                    num_bytes += len(msg)
                    k, _, v = msg.partition("=")
                    messages[k] = v

        with AsyncioThread(notify()):
            try:
                yield f"vsock-stream:{socket.VMADDR_CID_HOST}:{vsock.getsockname()[1]}", messages
            finally:
                logging.debug(
                    f"Received {num_messages} notify messages totalling {format_bytes(num_bytes)} bytes"
                )
                for k, v in messages.items():
                    logging.debug(f"- {k}={v}")


@contextlib.contextmanager
def start_journal_remote(config: Config, sockfd: int) -> Iterator[None]:
    assert config.forward_journal

    bin = config.find_binary("systemd-journal-remote", "/usr/lib/systemd/systemd-journal-remote")
    if not bin:
        die("systemd-journal-remote must be installed to forward logs from the virtual machine")

    if config.forward_journal.suffix == ".journal":
        d = config.forward_journal.parent
    else:
        d = config.forward_journal

    if not d.exists():
        # Pass exist_ok=True because multiple mkosi processes might be trying to create the parent directory
        # at the same time.
        d.mkdir(exist_ok=True, parents=True)
        # Make sure COW is disabled so systemd-journal-remote doesn't complain on btrfs filesystems.
        run(["chattr", "+C", d], check=False, stderr=subprocess.DEVNULL if not ARG_DEBUG.get() else None)
        INVOKING_USER.chown(d)

    with tempfile.NamedTemporaryFile(mode="w", prefix="mkosi-journal-remote-config-") as f:
        os.chmod(f.name, 0o644)

        # Make sure we capture all the logs by bumping the limits. We set MaxFileSize=4G because with the
        # compact mode enabled the files cannot grow any larger anyway.
        f.write(
            textwrap.dedent(
                f"""\
                [Remote]
                MaxUse=1T
                KeepFree=1G
                MaxFileSize=4G
                MaxFiles={1 if config.forward_journal.suffix == ".journal" else 100}
                """
            )
        )

        f.flush()

        user = d.stat().st_uid if os.getuid() == 0 else None
        group = d.stat().st_gid if os.getuid() == 0 else None
        scope = scope_cmd(
            name=f"mkosi-journal-remote-{config.machine_or_name()}",
            description=f"mkosi systemd-journal-remote for {config.machine_or_name()}",
            user=user,
            group=group,
        )

        with spawn(
            [
                bin,
                "--output", workdir(config.forward_journal),
                "--split-mode", "none" if config.forward_journal.suffix == ".journal" else "host",
            ],
            pass_fds=(sockfd,),
            sandbox=config.sandbox(
                binary=bin,
                options=[
                    "--bind", config.forward_journal.parent, workdir(config.forward_journal.parent),
                    "--ro-bind", f.name, "/etc/systemd/journal-remote.conf",
                ],
                setup=scope,
            ),
            user=user if not scope else None,
            group=group if not scope else None,
            foreground=False,
        ) as proc:  # fmt: skip
            yield
            proc.terminate()


@contextlib.contextmanager
def start_journal_remote_vsock(config: Config) -> Iterator[str]:
    with socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as sock:
        sock.bind((socket.VMADDR_CID_ANY, socket.VMADDR_PORT_ANY))
        sock.listen()

        with start_journal_remote(config, sock.fileno()):
            yield f"vsock-stream:{socket.VMADDR_CID_HOST}:{sock.getsockname()[1]}"


@contextlib.contextmanager
def copy_ephemeral(config: Config, src: Path) -> Iterator[Path]:
    if config.output_format in (OutputFormat.cpio, OutputFormat.uki):
        yield src
        return

    # If we're booting a directory image that was not built as root, we have to make an ephemeral copy so
    # that we can ensure the files in the directory are either owned by the actual root user or a fake one in
    # a subuid user namespace which we'll run virtiofsd as.
    if not config.ephemeral and (config.output_format != OutputFormat.directory or src.stat().st_uid == 0):
        with flock_or_die(src):
            yield src

        return

    src = src.resolve()
    # tempfile doesn't provide an API to get a random filename in an arbitrary directory so we do this
    # instead. Limit the size to 16 characters as the output name might be used in a unix socket path by
    # vmspawn and needs to fit in 108 characters.
    tmp = src.parent / f"{src.name}-{uuid.uuid4().hex[:16]}"

    try:

        def copy() -> None:
            if config.output_format == OutputFormat.directory:
                become_root_in_subuid_range()
            elif config.output_format in (OutputFormat.disk, OutputFormat.esp):
                attr = run(
                    ["lsattr", "-l", workdir(src)],
                    sandbox=config.sandbox(binary="lsattr", options=["--ro-bind", src, workdir(src)]),
                    stdout=subprocess.PIPE,
                ).stdout

                if "No_COW" in attr:
                    tmp.touch()
                    run(
                        ["chattr", "+C", workdir(tmp)],
                        sandbox=config.sandbox(binary="chattr", options=["--bind", tmp, workdir(tmp)]),
                    )

            copy_tree(
                src,
                tmp,
                # Make sure the ownership is changed to the (fake) root user if the directory was not built
                # as root.
                preserve=config.output_format == OutputFormat.directory and src.stat().st_uid == 0,
                use_subvolumes=config.use_subvolumes,
                sandbox=config.sandbox,
            )

        with flock(src):
            fork_and_wait(copy)
        yield tmp
    finally:

        def rm() -> None:
            if config.output_format == OutputFormat.directory:
                become_root_in_subuid_range()

            rmtree(tmp, sandbox=config.sandbox)

        fork_and_wait(rm)


def qemu_version(config: Config, binary: Path) -> GenericVersion:
    return GenericVersion(
        run(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            sandbox=config.sandbox(binary=binary),
        ).stdout.split()[3]
    )


def want_scratch(config: Config) -> bool:
    return config.runtime_scratch == ConfigFeature.enabled or (
        config.runtime_scratch == ConfigFeature.auto
        and config.find_binary(f"mkfs.{config.distribution.filesystem()}") is not None
    )


@contextlib.contextmanager
def generate_scratch_fs(config: Config) -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(dir="/var/tmp", prefix="mkosi-scratch-") as scratch:
        scratch.truncate(1024**4)
        fs = config.distribution.filesystem()
        extra = config.environment.get(f"SYSTEMD_REPART_MKFS_OPTIONS_{fs.upper()}", "")
        run(
            [f"mkfs.{fs}", "-L", "scratch", *extra.split(), workdir(Path(scratch.name))],
            stdout=subprocess.DEVNULL,
            sandbox=config.sandbox(
                binary=f"mkfs.{fs}",
                options=["--bind", scratch.name, workdir(Path(scratch.name))],
            ),
        )
        yield Path(scratch.name)


def finalize_qemu_firmware(config: Config, kernel: Optional[Path]) -> QemuFirmware:
    if config.qemu_firmware != QemuFirmware.auto:
        return config.qemu_firmware

    if kernel:
        if KernelType.identify(config, kernel) != KernelType.unknown:
            return QemuFirmware.uefi_secure_boot

        return QemuFirmware.linux

    if (
        config.output_format in (OutputFormat.cpio, OutputFormat.directory)
        or config.architecture.to_efi() is None
    ):
        return QemuFirmware.linux

    # At the moment there are no qemu firmware descriptions for non-x86 architectures that advertise
    # secure-boot support so let's default to no secure boot for non-x86 architectures.
    if config.architecture.is_x86_variant():
        return QemuFirmware.uefi_secure_boot

    return QemuFirmware.uefi


def finalize_firmware_variables(
    config: Config,
    qemu: Path,
    ovmf: OvmfConfig,
    stack: contextlib.ExitStack,
) -> tuple[Path, str]:
    ovmf_vars = stack.enter_context(tempfile.NamedTemporaryFile(prefix="mkosi-ovmf-vars-"))
    if config.qemu_firmware_variables in (None, Path("custom"), Path("microsoft")):
        ovmf_vars_format = ovmf.vars_format
    else:
        ovmf_vars_format = "raw"

    if config.qemu_firmware_variables == Path("custom"):
        assert config.secure_boot_certificate
        run(
            [
                "virt-fw-vars",
                "--input", workdir(ovmf.vars),
                "--output", workdir(Path(ovmf_vars.name)),
                "--enroll-cert", workdir(config.secure_boot_certificate),
                "--add-db", "OvmfEnrollDefaultKeys", workdir(config.secure_boot_certificate),
                "--no-microsoft",
                "--secure-boot",
                "--loglevel", "WARNING",
            ],
            sandbox=config.sandbox(
                binary=qemu,
                options=[
                    "--bind", ovmf_vars.name, workdir(Path(ovmf_vars.name)),
                    "--ro-bind", ovmf.vars, workdir(ovmf.vars),
                    "--ro-bind", config.secure_boot_certificate, workdir(config.secure_boot_certificate),
                ],
            ),
        )  # fmt: skip
    else:
        tools = (
            Path("/") if any(qemu.is_relative_to(d) for d in config.extra_search_paths) else config.tools()
        )
        vars = (
            tools / ovmf.vars.relative_to("/")
            if config.qemu_firmware_variables == Path("microsoft") or not config.qemu_firmware_variables
            else config.qemu_firmware_variables
        )
        shutil.copy2(vars, Path(ovmf_vars.name))

    return Path(ovmf_vars.name), ovmf_vars_format


def apply_runtime_size(config: Config, image: Path) -> None:
    if config.output_format != OutputFormat.disk or not config.runtime_size:
        return

    run(
        [
            "systemd-repart",
            "--definitions=/",
            "--no-pager",
            # To use qemu's cache.direct option, the drive size has to be a multiple of the page size.
            f"--size={round_up(config.runtime_size, resource.getpagesize())}",
            "--pretty=no",
            "--offline=yes",
            workdir(image),
        ],
        sandbox=config.sandbox(binary="systemd-repart", options=["--bind", image, workdir(image)]),
    )  # fmt: skip


@contextlib.contextmanager
def finalize_drive(drive: QemuDrive) -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(
        dir=drive.directory or "/var/tmp", prefix=f"mkosi-drive-{drive.id}"
    ) as file:
        file.truncate(drive.size)
        yield Path(file.name)


@contextlib.contextmanager
def finalize_state(config: Config, cid: int) -> Iterator[None]:
    (INVOKING_USER.runtime_dir() / "machine").mkdir(parents=True, exist_ok=True)

    with flock(INVOKING_USER.runtime_dir() / "machine"):
        if (p := INVOKING_USER.runtime_dir() / "machine" / f"{config.machine_or_name()}.json").exists():
            die(
                f"Another virtual machine named {config.machine_or_name()} is already running",
                hint="Use --machine to specify a different virtual machine name",
            )

        p.write_text(
            json.dumps(
                {
                    "Machine": config.machine_or_name(),
                    "ProxyCommand": f"socat - VSOCK-CONNECT:{cid}:%p",
                    "SshKey": os.fspath(config.ssh_key) if config.ssh_key else None,
                },
                sort_keys=True,
                indent=4,
            )
        )
    try:
        yield
    finally:
        with flock(INVOKING_USER.runtime_dir() / "machine"):
            p.unlink(missing_ok=True)


def finalize_kernel_command_line_extra(config: Config) -> list[str]:
    columns, lines = shutil.get_terminal_size()
    term = finalize_term()

    cmdline = [
        "rw",
        # Make sure we set up networking in the VM/container.
        "systemd.wants=network.target",
        # Make sure we don't load vmw_vmci which messes with virtio vsock.
        "module_blacklist=vmw_vmci",
        f"systemd.tty.term.hvc0={term}",
        f"systemd.tty.columns.hvc0={columns}",
        f"systemd.tty.rows.hvc0={lines}",
    ]

    if not any(s.startswith("ip=") for s in config.kernel_command_line_extra):
        cmdline += ["ip=enc0:any", "ip=enp0s1:any", "ip=enp0s2:any", "ip=host0:any", "ip=none"]

    if not any(s.startswith("loglevel=") for s in config.kernel_command_line_extra):
        cmdline += ["loglevel=4"]

    if not any(s.startswith("SYSTEMD_SULOGIN_FORCE=") for s in config.kernel_command_line_extra):
        cmdline += ["SYSTEMD_SULOGIN_FORCE=1"]

    if (
        not any(s.startswith("systemd.hostname=") for s in config.kernel_command_line_extra)
        and config.machine
    ):
        cmdline += [f"systemd.hostname={config.machine}"]

    if config.qemu_cdrom:
        # CD-ROMs are read-only so tell systemd to boot in volatile mode.
        cmdline += ["systemd.volatile=yes"]

    if not config.qemu_gui:
        cmdline += [
            f"systemd.tty.term.console={term}",
            f"systemd.tty.columns.console={columns}",
            f"systemd.tty.rows.console={lines}",
            "console=hvc0",
            f"TERM={term}",
        ]

    for s in config.kernel_command_line_extra:
        key, sep, value = s.partition("=")
        if " " in value:
            value = f'"{value}"'
        cmdline += [key if not sep else f"{key}={value}"]

    return cmdline


def finalize_credentials(config: Config) -> dict[str, str]:
    creds = {
        "firstboot.locale": "C.UTF-8",
        **config.credentials,
    }

    if "firstboot.timezone" not in creds:
        if find_binary("timedatectl"):
            tz = run(
                ["timedatectl", "show", "-p", "Timezone", "--value"],
                stdout=subprocess.PIPE,
                check=False,
            ).stdout.strip()
        else:
            tz = "UTC"

        creds["firstboot.timezone"] = tz

    if "ssh.authorized_keys.root" not in creds:
        if config.ssh_certificate:
            pubkey = run(
                ["openssl", "x509", "-in", config.ssh_certificate, "-pubkey", "-noout"],
                stdout=subprocess.PIPE,
                env=dict(OPENSSL_CONF="/dev/null"),
            ).stdout.strip()
            sshpubkey = run(
                ["ssh-keygen", "-f", "/dev/stdin", "-i", "-m", "PKCS8"], input=pubkey, stdout=subprocess.PIPE
            ).stdout.strip()
            creds["ssh.authorized_keys.root"] = sshpubkey
        elif config.ssh:
            die(
                "Ssh= is enabled but no SSH certificate was found",
                hint="Run 'mkosi genkey' to automatically create one",
            )

    return creds


def scope_cmd(
    name: str,
    description: str,
    user: Optional[int] = None,
    group: Optional[int] = None,
    properties: Sequence[str] = (),
    environment: bool = True,
) -> list[str]:
    if not find_binary("systemd-run"):
        return []

    if os.getuid() != 0 and "DBUS_SESSION_BUS_ADDRESS" in os.environ and "XDG_RUNTIME_DIR" in os.environ:
        env = {
            "DBUS_SESSION_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"],
            "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
        }
    elif os.getuid() == 0:
        if "DBUS_SYSTEM_ADDRESS" in os.environ:
            env = {"DBUS_SYSTEM_ADDRESS": os.environ["DBUS_SYSTEM_ADDRESS"]}
        elif Path("/run/dbus/system_bus_socket").exists():
            env = {"DBUS_SYSTEM_ADDRESS": "/run/dbus/system_bus_socket"}
        else:
            return []
    else:
        return []

    return [
        "env",
        *(f"{k}={v}" for k, v in env.items() if environment),
        "systemd-run",
        "--system" if os.getuid() == 0 else "--user",
        *(["--quiet"] if not ARG_DEBUG.get() else []),
        "--unit", name,
        "--description", description,
        "--scope",
        "--collect",
        *(["--expand-environment=no"] if systemd_tool_version("systemd-run") >= 254 else []),
        *(["--uid", str(user)] if user is not None else []),
        *(["--gid", str(group)] if group is not None else []),
        *([f"--property={p}" for p in properties]),
    ]  # fmt: skip


def register_machine(config: Config, pid: int, fname: Path) -> None:
    if os.getuid() != 0 or (
        "DBUS_SYSTEM_ADDRESS" not in os.environ and not Path("/run/dbus/system_bus_socket").exists()
    ):
        return

    run(
        [
            "busctl",
            "call",
            "--quiet",
            "org.freedesktop.machine1",
            "/org/freedesktop/machine1",
            "org.freedesktop.machine1.Manager",
            "RegisterMachine",
            "sayssus",
            config.machine_or_name().replace("_", "-"),
            "0",
            "mkosi",
            "vm",
            str(pid),
            fname if fname.is_dir() else "",
        ],  # fmt: skip
        foreground=False,
        env=os.environ | config.environment,
        sandbox=config.sandbox(binary="busctl", relaxed=True),
        # systemd-machined might not be installed so let's ignore any failures unless running in debug mode.
        check=ARG_DEBUG.get(),
        stderr=None if ARG_DEBUG.get() else subprocess.DEVNULL,
    )


def run_qemu(args: Args, config: Config) -> None:
    if config.output_format not in (
        OutputFormat.disk,
        OutputFormat.cpio,
        OutputFormat.uki,
        OutputFormat.esp,
        OutputFormat.directory,
    ):
        die(f"{config.output_format} images cannot be booted in qemu")

    if (
        config.output_format in (OutputFormat.cpio, OutputFormat.uki, OutputFormat.esp)
        and config.qemu_firmware not in (QemuFirmware.auto, QemuFirmware.linux)
        and not config.qemu_firmware.is_uefi()
    ):
        die(f"{config.output_format} images cannot be booted with the '{config.qemu_firmware}' firmware")

    if config.runtime_trees and config.qemu_firmware == QemuFirmware.bios:
        die("RuntimeTrees= cannot be used when booting in BIOS firmware")

    if config.qemu_kvm == ConfigFeature.enabled and not config.architecture.is_native():
        die(
            f"KVM acceleration requested but {config.architecture} does not match "
            "the native host architecture"
        )

    if config.qemu_firmware_variables == Path("custom") and not config.secure_boot_certificate:
        die("SecureBootCertificate= must be configured to use QemuFirmwareVariables=custom")

    # After we unshare the user namespace to sandbox qemu, we might not have access to /dev/kvm or related
    # device nodes anymore as access to these might be gated behind the kvm group and we won't be part of the
    # kvm group anymore after unsharing the user namespace. To get around this, open all those device nodes
    # early can pass them as file descriptors to qemu later. Note that we can't pass the kvm file descriptor
    # to qemu until version 9.0.
    qemu_device_fds = {
        d: d.open()
        for d in QemuDeviceNode
        if d.feature(config) != ConfigFeature.disabled and d.available(log=True)
    }

    if not (qemu := config.find_binary(f"qemu-system-{config.architecture.to_qemu()}")):
        die(
            "qemu not found.",
            hint=f"Is qemu-system-{config.architecture.to_qemu()} installed on the host system?",
        )

    have_kvm = (qemu_version(config, qemu) < QEMU_KVM_DEVICE_VERSION and QemuDeviceNode.kvm.available()) or (
        qemu_version(config, qemu) >= QEMU_KVM_DEVICE_VERSION and QemuDeviceNode.kvm in qemu_device_fds
    )
    if config.qemu_kvm == ConfigFeature.enabled and not have_kvm:
        die("KVM acceleration requested but cannot access /dev/kvm")

    if config.qemu_vsock == ConfigFeature.enabled and QemuDeviceNode.vhost_vsock not in qemu_device_fds:
        die("VSock requested but cannot access /dev/vhost-vsock")

    if config.qemu_kernel:
        kernel = config.qemu_kernel
    elif "-kernel" in args.cmdline:
        kernel = Path(args.cmdline[args.cmdline.index("-kernel") + 1])
    else:
        kernel = None

    if config.output_format in (OutputFormat.uki, OutputFormat.esp) and kernel:
        logging.warning(
            f"Booting UKI output, kernel {kernel} configured with QemuKernel= or "
            "passed with -kernel will not be used"
        )
        kernel = None

    if kernel and not kernel.exists():
        die(f"Kernel not found at {kernel}")

    firmware = finalize_qemu_firmware(config, kernel)

    if not kernel and (
        firmware == QemuFirmware.linux
        or config.output_format in (OutputFormat.cpio, OutputFormat.directory, OutputFormat.uki)
    ):
        if firmware.is_uefi():
            name = config.output if config.output_format == OutputFormat.uki else config.output_split_uki
            kernel = config.output_dir_or_cwd() / name
        else:
            kernel = config.output_dir_or_cwd() / config.output_split_kernel
        if not kernel.exists():
            die(
                f"Kernel or UKI not found at {kernel}, please install a kernel in the image "
                "or provide a -kernel argument to mkosi qemu"
            )

    ovmf = find_ovmf_firmware(config, qemu, firmware)

    # A shared memory backend might increase ram usage so only add one if actually necessary for virtiofsd.
    shm = []
    if (
        config.runtime_trees
        or config.runtime_build_sources
        or config.runtime_home
        or config.output_format == OutputFormat.directory
    ):
        shm = ["-object", f"memory-backend-memfd,id=mem,size={config.qemu_mem // 1024**2}M,share=on"]

    machine = f"type={config.architecture.default_qemu_machine()}"
    if firmware.is_uefi() and config.architecture.supports_smm():
        machine += f",smm={'on' if firmware == QemuFirmware.uefi_secure_boot else 'off'}"
    if shm:
        machine += ",memory-backend=mem"

    cmdline: list[PathString] = [
        qemu,
        "-machine", machine,
        "-smp", str(config.qemu_smp or os.cpu_count()),
        "-m", f"{config.qemu_mem // 1024**2}M",
        "-object", "rng-random,filename=/dev/urandom,id=rng0",
        "-device", "virtio-rng-pci,rng=rng0,id=rng-device0",
        "-device", "virtio-balloon,free-page-reporting=on",
        "-no-user-config",
        *shm,
    ]  # fmt: skip

    if config.runtime_network == Network.user:
        cmdline += ["-nic", f"user,model={config.architecture.default_qemu_nic_model()}"]
    elif config.runtime_network == Network.interface:
        if os.getuid() != 0:
            die("RuntimeNetwork=interface requires root privileges")

        cmdline += ["-nic", "tap,script=no,model=virtio-net-pci"]
    elif config.runtime_network == Network.none:
        cmdline += ["-nic", "none"]

    if config.qemu_kvm != ConfigFeature.disabled and have_kvm and config.architecture.can_kvm():
        accel = "kvm"
        if qemu_version(config, qemu) >= QEMU_KVM_DEVICE_VERSION:
            index = list(qemu_device_fds.keys()).index(QemuDeviceNode.kvm)
            cmdline += ["--add-fd", f"fd={SD_LISTEN_FDS_START + index},set=1,opaque=/dev/kvm"]
            accel += ",device=/dev/fdset/1"
    else:
        accel = "tcg"

    cmdline += ["-accel", accel]

    cid: Optional[int] = None
    if QemuDeviceNode.vhost_vsock in qemu_device_fds:
        if config.qemu_vsock_cid == QemuVsockCID.auto:
            cid = find_unused_vsock_cid(config, qemu_device_fds[QemuDeviceNode.vhost_vsock])
        elif config.qemu_vsock_cid == QemuVsockCID.hash:
            cid = hash_to_vsock_cid(hash_output(config))
        else:
            cid = config.qemu_vsock_cid

        if vsock_cid_in_use(qemu_device_fds[QemuDeviceNode.vhost_vsock], cid):
            die(
                f"VSock connection ID {cid} is already in use by another virtual machine",
                hint="Use QemuVsockConnectionId=auto to have mkosi automatically "
                "find a free vsock connection ID",
            )

        index = list(qemu_device_fds.keys()).index(QemuDeviceNode.vhost_vsock)
        cmdline += ["-device", f"vhost-vsock-pci,guest-cid={cid},vhostfd={SD_LISTEN_FDS_START + index}"]

    cmdline += ["-cpu", "max"]

    if config.qemu_gui:
        cmdline += ["-vga", "virtio"]
    else:
        # -nodefaults removes the default CDROM device which avoids an error message during boot
        # -serial mon:stdio adds back the serial device removed by -nodefaults.
        cmdline += [
            "-nographic",
            "-nodefaults",
            "-chardev", "stdio,mux=on,id=console,signal=off",
            "-device", "virtio-serial-pci,id=mkosi-virtio-serial-pci",
            "-device", "virtconsole,chardev=console",
            "-mon", "console",
        ]  # fmt: skip

    # QEMU has built-in logic to look for the BIOS firmware so we don't need to do anything special for that.
    if firmware.is_uefi():
        assert ovmf
        cmdline += ["-drive", f"if=pflash,format={ovmf.format},readonly=on,file={ovmf.firmware}"]
    notifications: dict[str, str] = {}

    with contextlib.ExitStack() as stack:
        if firmware.is_uefi():
            assert ovmf
            ovmf_vars, ovmf_vars_format = finalize_firmware_variables(config, qemu, ovmf, stack)

            cmdline += ["-drive", f"file={ovmf_vars},if=pflash,format={ovmf_vars_format}"]
            if firmware == QemuFirmware.uefi_secure_boot:
                cmdline += [
                    "-global", "ICH9-LPC.disable_s3=1",
                    "-global", "driver=cfi.pflash01,property=secure,value=on",
                ]  # fmt: skip

        if config.qemu_cdrom and config.output_format in (OutputFormat.disk, OutputFormat.esp):
            # CD-ROM devices have sector size 2048 so we transform disk images into ones with sector size
            # 2048.
            src = (config.output_dir_or_cwd() / config.output_with_compression).resolve()
            fname = src.parent / f"{src.name}-{uuid.uuid4().hex}"
            run(
                [
                    "systemd-repart",
                    "--definitions=/",
                    "--no-pager",
                    "--pretty=no",
                    "--offline=yes",
                    "--empty=create",
                    "--size=auto",
                    "--sector-size=2048",
                    "--copy-from", workdir(src),
                    workdir(fname),
                ],  # fmt: skip
                sandbox=config.sandbox(
                    binary="systemd-repart",
                    options=[
                        "--bind", fname.parent, workdir(fname.parent),
                        "--ro-bind", src, workdir(src),
                    ],
                ),
            )  # fmt: skip
            stack.callback(lambda: fname.unlink())
        else:
            fname = stack.enter_context(
                copy_ephemeral(config, config.output_dir_or_cwd() / config.output_with_compression)
            )

        apply_runtime_size(config, fname)

        if kernel and (
            KernelType.identify(config, kernel) != KernelType.uki
            or not config.architecture.supports_smbios(firmware)
        ):
            kcl = config.kernel_command_line + finalize_kernel_command_line_extra(config)
        else:
            kcl = finalize_kernel_command_line_extra(config)

        if kernel:
            cmdline += ["-kernel", kernel]

            if any(s.startswith("root=") for s in kcl):
                pass
            elif config.output_format == OutputFormat.disk:
                # We can't rely on gpt-auto-generator when direct kernel booting so synthesize a root=
                # kernel argument instead.
                root = finalize_root(find_partitions(fname, sandbox=config.sandbox))
                if not root:
                    die("Cannot perform a direct kernel boot without a root or usr partition")

                kcl += [root]
            elif config.output_format == OutputFormat.directory:
                sock = stack.enter_context(
                    start_virtiofsd(
                        config,
                        fname,
                        name=config.machine_or_name(),
                        uidmap=False,
                        selinux=bool(want_selinux_relabel(config, fname, fatal=False)),
                    ),
                )
                cmdline += [
                    "-chardev", f"socket,id={sock.name},path={sock}",
                    "-device", f"vhost-user-fs-pci,queue-size=1024,chardev={sock.name},tag=root",
                ]  # fmt: skip
                kcl += ["root=root", "rootfstype=virtiofs"]

        credentials = finalize_credentials(config)

        def add_virtiofs_mount(
            sock: Path, dst: PathString, cmdline: list[PathString], credentials: dict[str, str], *, tag: str
        ) -> None:
            cmdline += [
                "-chardev", f"socket,id={sock.name},path={sock}",
                "-device", f"vhost-user-fs-pci,queue-size=1024,chardev={sock.name},tag={tag}",
            ]  # fmt: skip

            if "fstab.extra" not in credentials:
                credentials["fstab.extra"] = ""

            if credentials["fstab.extra"] and not credentials["fstab.extra"][-1] == "\n":
                credentials["fstab.extra"] += "\n"

            credentials["fstab.extra"] += f"{tag} {dst} virtiofs x-initrd.mount\n"

        if config.runtime_build_sources:
            for t in config.build_sources:
                src, dst = t.with_prefix("/work/src")
                sock = stack.enter_context(start_virtiofsd(config, src))
                add_virtiofs_mount(sock, dst, cmdline, credentials, tag=src.name)

            if config.build_dir:
                sock = stack.enter_context(start_virtiofsd(config, config.build_dir))
                add_virtiofs_mount(sock, "/work/build", cmdline, credentials, tag="build")

        for tree in config.runtime_trees:
            sock = stack.enter_context(start_virtiofsd(config, tree.source))
            add_virtiofs_mount(
                sock,
                Path("/root/src") / (tree.target or ""),
                cmdline,
                credentials,
                tag=tree.target.name if tree.target else tree.source.name,
            )

        if config.runtime_home and (p := current_home_dir()):
            sock = stack.enter_context(start_virtiofsd(config, p))
            add_virtiofs_mount(
                sock,
                Path("/root"),
                cmdline,
                credentials,
                tag="user-home",
            )

        if want_scratch(config) or config.output_format in (OutputFormat.disk, OutputFormat.esp):
            cmdline += ["-device", "virtio-scsi-pci,id=mkosi"]

        if want_scratch(config):
            scratch = stack.enter_context(generate_scratch_fs(config))
            cache = "cache.writeback=on,cache.direct=on,cache.no-flush=yes,aio=io_uring"
            cmdline += [
                "-drive", f"if=none,id=scratch,file={scratch},format=raw,discard=on,{cache}",
                "-device", "scsi-hd,drive=scratch",
            ]  # fmt: skip
            kcl += [f"systemd.mount-extra=LABEL=scratch:/var/tmp:{config.distribution.filesystem()}"]

        if config.output_format == OutputFormat.cpio:
            cmdline += ["-initrd", fname]
        elif (
            kernel
            and KernelType.identify(config, kernel) != KernelType.uki
            and "-initrd" not in args.cmdline
            and (config.output_dir_or_cwd() / config.output_split_initrd).exists()
        ):
            cmdline += ["-initrd", config.output_dir_or_cwd() / config.output_split_initrd]

        if config.output_format in (OutputFormat.disk, OutputFormat.esp):
            direct = fname.stat().st_size % resource.getpagesize() == 0
            ephemeral = config.ephemeral
            cache = f"cache.writeback=on,cache.direct={yes_no(direct)},cache.no-flush={yes_no(ephemeral)},aio=io_uring"  # noqa
            cmdline += [
                "-drive", f"if=none,id=mkosi,file={fname},format=raw,discard=on,{cache}",
                "-device", f"scsi-{'cd' if config.qemu_cdrom else 'hd'},drive=mkosi,bootindex=1",
            ]  # fmt: skip

        if config.qemu_swtpm == ConfigFeature.enabled or (
            config.qemu_swtpm == ConfigFeature.auto
            and firmware.is_uefi()
            and config.find_binary("swtpm") is not None
        ):
            sock = stack.enter_context(start_swtpm(config))
            cmdline += [
                "-chardev", f"socket,id=chrtpm,path={sock}",
                "-tpmdev", "emulator,id=tpm0,chardev=chrtpm",
            ]  # fmt: skip

            if config.architecture.is_x86_variant():
                cmdline += ["-device", "tpm-tis,tpmdev=tpm0"]
            elif config.architecture.is_arm_variant():
                cmdline += ["-device", "tpm-tis-device,tpmdev=tpm0"]

        if QemuDeviceNode.vhost_vsock in qemu_device_fds:
            addr, notifications = stack.enter_context(vsock_notify_handler())
            credentials["vmm.notify_socket"] = addr

        if config.forward_journal:
            credentials["journal.forward_to_socket"] = stack.enter_context(
                start_journal_remote_vsock(config)
            )

        for k, v in credentials.items():
            payload = base64.b64encode(v.encode()).decode()
            if config.architecture.supports_smbios(firmware):
                cmdline += ["-smbios", f"type=11,value=io.systemd.credential.binary:{k}={payload}"]
            # qemu's fw_cfg device only supports keys up to 55 characters long.
            elif config.architecture.supports_fw_cfg() and len(k) <= 55 - len("opt/io.systemd.credentials/"):
                f = stack.enter_context(tempfile.NamedTemporaryFile(prefix="mkosi-fw-cfg-", mode="w"))
                f.write(v)
                f.flush()
                cmdline += ["-fw_cfg", f"name=opt/io.systemd.credentials/{k},file={f.name}"]
            elif kernel:
                kcl += [f"systemd.set_credential_binary={k}:{payload}"]

        if kernel and (
            KernelType.identify(config, kernel) != KernelType.uki
            or not config.architecture.supports_smbios(firmware)
        ):
            cmdline += ["-append", " ".join(kcl)]
        elif config.architecture.supports_smbios(firmware):
            cmdline += [
                "-smbios",
                f"type=11,value=io.systemd.stub.kernel-cmdline-extra={' '.join(kcl).replace(',', ',,')}",
                "-smbios",
                f"type=11,value=io.systemd.boot.kernel-cmdline-extra={' '.join(kcl).replace(',', ',,')}",
            ]

        for _, drives in groupby(config.qemu_drives, key=lambda d: d.file_id):
            file = stack.enter_context(finalize_drive(drives[0]))

            for drive in drives:
                arg = f"if=none,id={drive.id},file={file},format=raw,file.locking=off"
                if drive.options:
                    arg += f",{drive.options}"

                cmdline += ["-drive", arg]

        cmdline += config.qemu_args
        cmdline += args.cmdline

        if cid is not None:
            stack.enter_context(finalize_state(config, cid))

        # Reopen stdin, stdout and stderr to give qemu a private copy of them.  This is a mitigation for the
        # case when running mkosi under meson and one or two of the three are redirected and their pipe might
        # block, but qemu opens all of them non-blocking because at least one of them is opened this way.
        stdin = try_or(
            lambda: os.open(f"/proc/self/fd/{sys.stdin.fileno()}", os.O_RDONLY),
            OSError,
            sys.stdin.fileno(),
        )
        stdout = try_or(
            lambda: os.open(f"/proc/self/fd/{sys.stdout.fileno()}", os.O_WRONLY),
            OSError,
            sys.stdout.fileno(),
        )
        stderr = try_or(
            lambda: os.open(f"/proc/self/fd/{sys.stderr.fileno()}", os.O_WRONLY),
            OSError,
            sys.stderr.fileno(),
        )

        name = f"mkosi-{config.machine_or_name().replace('_', '-')}"
        with spawn(
            cmdline,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=qemu_device_fds.values(),
            env=os.environ | config.environment,
            foreground=True,
            sandbox=config.sandbox(
                binary=qemu,
                network=True,
                devices=True,
                relaxed=True,
                setup=scope_cmd(
                    name=name,
                    description=f"mkosi Virtual Machine {name}",
                    properties=config.unit_properties,
                    environment=False,
                ),
            ),
        ) as proc:
            # We have to close these before we wait for qemu otherwise we'll deadlock as qemu will never
            # exit.
            for fd in qemu_device_fds.values():
                os.close(fd)

            register_machine(config, proc.pid, fname)

        if status := int(notifications.get("EXIT_STATUS", 0)):
            raise subprocess.CalledProcessError(status, cmdline)


def run_ssh(args: Args, config: Config) -> None:
    with flock(INVOKING_USER.runtime_dir() / "machine"):
        if not (p := INVOKING_USER.runtime_dir() / "machine" / f"{config.machine_or_name()}.json").exists():
            die(
                f"{p} not found, cannot SSH into virtual machine {config.machine_or_name()}",
                hint="Is the machine running and was it built with Ssh=yes and QemuVsock=yes?",
            )

        state = json.loads(p.read_text())

    if not state["SshKey"]:
        die(
            "An SSH key must be configured when booting the image to use 'mkosi ssh'",
            hint="Use 'mkosi genkey' to generate a new SSH key and certificate",
        )

    cmd: list[PathString] = [
        "ssh",
        "-i", state["SshKey"],
        "-F", "none",
        # Silence known hosts file errors/warnings.
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=no",
        "-o", "LogLevel=ERROR",
        "-o", f"ProxyCommand={state['ProxyCommand']}",
        "root@mkosi",
    ]  # fmt: skip

    cmd += args.cmdline

    run(
        cmd,
        stdin=sys.stdin,
        stdout=sys.stdout,
        env=os.environ | config.environment,
        log=False,
        sandbox=config.sandbox(binary="ssh", network=True, devices=True, relaxed=True),
    )
