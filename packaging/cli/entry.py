"""PyInstaller entry for the standalone CLI."""

if __name__ == "__main__":
    from mobile_agent import build_identity

    build_identity.STANDALONE_CLI = True
    from mobile_agent.__main__ import main

    raise SystemExit(main())
