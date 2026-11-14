{ config, lib, pkgs, op, ... }:

{
  programs.claude-code = {
    enable = true;
    # Let Claude save interactive preferences; declared values win on activation.
    mutableSettings = true;
    settings = {
      advisorModel = "fable";
      attribution = false;
      autoCompactEnabled = true;
      autoCompactWindow = 500000;
      # TODO: add autoMemoryDirectory, pointing to a cloud store
      autoMemoryEnabled = true;
      autoScrollEnabled = true;
      awaySummaryEnabled = true;
      channelsEnabled = true;
      copyOnSelect = true;
      effortLevel = "medium";
      enableArtifact = true;
      enableWorkflows = true;
      feedbackDrafts = "off";
      idleCompaction = true;
      outputStyle = "Concise";
      showThinkingSummaries = true;
      promptCacheTtl = "1h";
      subagentPromptCacheTtl = "1h";
      respondToBashCommands = false;
      showTurnDuration = true;
      showClearContextOnPlanAccept = true;
      skipAutoPermissionPrompt = true;
      switchModelsOnFlag = false;
      terminalProgressBarEnabled = true;
      tui = "fullscreen";
      workflowSizeGuideline = "small";
      modelSettings = {
        claude-opus-5-5 = {
          effortLevel = "xhigh";
        };
        claude-fable-5-2 = {
          effortLevel = "high";
        };
        claude-mythos-5-2 = {
          effortLevel = "high";
        };
      };
      permissions = {
        defaultMode = "auto";
      };
      model = "claude-opus-5-5";
      env = {
        DISABLE_AUTOUPDATER = "1";
        CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS = "100000";
        # force clone local dir instead of git remote pull when running claude --cloud
        CCR_FORCE_BUNDLE = "1";
        CLAUDE_CODE_PROMPT_SUGGESTION_ENABLED = "true";
        CLAUDE_CODE_ENABLE_TASKS = "1";
        CLAUDE_CODE_ENABLE_TODO_TOOLS = "1";
        CLAUDE_CODE_EXPERIMENTAL_CODE_MODE = "1";
        CLAUDE_CODE_FORWARD_SUBAGENT_TEXT = "1";

        # concurrency & subagent limits
        CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS = "10";
        CLAUDE_CODE_MAX_OUTPUT_TOKENS = "128000";
        CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH = "1";

        CLAUDE_CODE_NATIVE_CURSOR = "1";
        CLAUDE_CODE_NEW_INIT = "1";
        CLAUDE_CODE_NONBLOCKING_STDOUT = "1";
        CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT = "1";
        ENABLE_TOOL_SEARCH = "true";
      };
      statusLine = {
        command = "input=$(cat); echo \"[$(echo \"$input\" | jq -r '.model.display_name')] 📁 $(basename \"$(echo \"$input\" | jq -r '.workspace.current_dir')\")\"";
        padding = 0;
        type = "command";
      };
    };

    # Install one claude command, which always resolves the token at runtime.
    package = (pkgs.writeShellApplication {
      name = "claude";
      text = builtins.replaceStrings
        [ "@op@" "@claude@" ]
        [ "${op}/bin/op" "${pkgs.claude-code}/bin/claude" ]
        (builtins.readFile ./scripts/claude.sh);
    }).overrideAttrs {
      version = pkgs.claude-code.version;
    };
  };

  # Needed to allow for interactive runs when using CLAUDE_CODE_OAUTH_TOKEN
  # This activation trigger allows .claude.json to remain mutable.
  # The sandbox workspaces are also trusted, so Claude starts without asking.
  home.activation.claudeOnboarding =
    lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${lib.getExe (pkgs.writeShellApplication {
        name = "claude-onboarding";
        runtimeInputs = [ pkgs.coreutils pkgs.jq ];
        text = builtins.readFile ./scripts/claude-onboarding.sh;
      })} ${lib.escapeShellArgs ([ "${config.home.homeDirectory}/.claude.json" ]
        ++ lib.optionals pkgs.stdenv.hostPlatform.isLinux [ "/root" "/workdir" ])}
    '';
}
