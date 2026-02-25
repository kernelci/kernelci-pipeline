def test_shell_available(shell):
    _, _, rc = shell.run("echo hello")
    assert rc == 0


def test_kernel_version(shell):
    stdout, _, rc = shell.run("cat /proc/version")
    assert rc == 0
    print("".join(stdout).strip())


def test_kernel_cmdline(shell):
    stdout, _, rc = shell.run("cat /proc/cmdline")
    assert rc == 0
    assert "".join(stdout).strip()


def test_device_is_up(shell, kci_environment):
    stdout, _, rc = shell.run("uptime")
    assert rc == 0
    platform = kci_environment.get("platform", "unknown")
    arch = kci_environment.get("arch", "unknown")
    print(f"{platform}/{arch}: {''.join(stdout).strip()}")
