{
  # Claude starts in these directories without asking. Keep /workdir in step
  # with WORKDIR in src/agentix/image.py.
  agentix.claude.trustedDirectories = [
    "/root"
    "/workdir"
  ];

  # Loaded into every session as ~/.claude/CLAUDE.md.
  programs.claude-code.context = ./claude-context.md;
}
