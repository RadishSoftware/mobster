"""Track `files`: files attached to tasks and conversations, and files to and from the phone (docs/files.md).

store.py keeps them, sniff.py and extract.py say what a file is and read it, agent.py gives Mobster's agent the
attachments block and the READ_ATTACHMENT and PUT_FILE tools, routes.py and mcp.py are the HTTP and MCP surfaces, and
service.py ties them to a running Mobster. phone_io/files.py moves files to and from the phone. Only the files track
edits this package."""
