"""What the `mobster` command and its argument parser (cli_parser.py) both need, so neither imports the other."""

DEFAULT_WDA_URL = "http://127.0.0.1:8100"
# Exit codes: 0 done, 1 the task or check did not succeed, 2 usage, 3 couldn't run (no phone, no key).
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_COULDNT = 0, 1, 2, 3
