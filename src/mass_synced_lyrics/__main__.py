import sys


def main() -> None:
    if sys.argv[1:2] == ["--backend"]:          # the desktop app's backend (started by the window)
        from .server import main as server_main
        server_main()
        return
    from .cli import main as cli_main
    sys.exit(cli_main())


if __name__ == "__main__":
    main()
