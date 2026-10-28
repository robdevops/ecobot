"""Where the bot runs, for the hints in its log. In a Docker container on one of Docker's own networks a .local name (mDNS) cannot be
found: the fix is the device's IP address in the setting, or the host's network for the container (README: Docker networking)."""

import os

NET = "/sys/class/net"


def docker_private_network() -> bool:
    """In a Docker container that is not on the host's network? The host's network has Docker's own bridges on it (docker0, br-...),
    which a container on a network of Docker's never sees."""
    if not os.path.exists("/.dockerenv"):
        return False
    try:
        names = os.listdir(NET)
    except OSError:
        return False
    return not any(name.startswith(("docker", "br-", "veth")) for name in names)


def local_name_hint(host: str, setting: str) -> str:
    """What to do about a .local name that can't be reached from Docker's private network ("" when that is not the case)."""
    if not host.endswith(".local") or not docker_private_network():
        return ""
    return (f". Docker's private network can't find .local names: put the IP address in {setting}, or put the container on the host "
            "network (README: Docker networking)")
