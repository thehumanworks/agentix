{ inputs }:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.claude;
  inherit (config.agentix.op) tokenFile;

  # op loads the token from the file, so the commands Claude runs do not need
  # it in their environment. A different exported token is an override and
  # stays.
  dropOpToken = lib.optionalString (tokenFile != null) ''
    token_file=''${AGENTIX_OP_TOKEN_FILE:-${tokenFile}}
    if [[ -r $token_file ]] &&
      [[ -z ''${OP_SERVICE_ACCOUNT_TOKEN:-} || $OP_SERVICE_ACCOUNT_TOKEN == "$(<"$token_file")" ]]; then
      launch+=(${lib.getExe' pkgs.coreutils "env"} -u OP_SERVICE_ACCOUNT_TOKEN)
    fi
  '';
in
{
  imports = [ ./1password.nix ];

  options.agentix.claude = {
    enable = lib.mkEnableOption "Claude Code, with its settings, plugins, and `claude` wrapper" // {
      default = true;
    };

    trustedDirectories = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "/srv/project" ];
      description = "Directories where Claude starts without the workspace trust prompt.";
    };
  };

  config = lib.mkIf cfg.enable {
    agentix.unfreePackages = [ "claude-code" ];
    agentix.commands = [ "claude" ];

    programs.claude-code = {
      enable = true;
      # Let Claude save interactive preferences; declared values win on activation.
      mutableSettings = true;
      # The flake input pins the commit, so the image holds the plugins and
      # Claude loads them from the store without an install step. The name must
      # match the one in the repository's marketplace.json.
      marketplaces.claude-mods = inputs.claude-mods.outPath;
      settings = {
        enabledPlugins = {
          "async-compaction@claude-mods" = true;
          "turn-stats@claude-mods" = true;
        };
        advisorModel = "fable";
        attribution = false;
        autoCompactEnabled = true;
        autoCompactWindow = 400000;
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
          CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS = "1";
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
      # The version is kept so Home Manager picks the plugin layout for it.
      package =
        (pkgs.writeShellApplication {
          name = "claude";
          text = ''
            # Modal sandboxes start with /tmp as 0777, and a mode set during the
            # image build does not survive into them. Claude refuses to put its
            # cross-session messaging socket in a world-writable directory that
            # lacks the sticky bit, so restore the standard 1777 when we own /tmp.
            if [[ -O /tmp && ! -k /tmp ]]; then
              chmod +t /tmp || true
            fi

            export CLAUDE_CODE_OAUTH_TOKEN="op://agentix/claude/oauth/token"
            launch=(${lib.getExe config.agentix.op.package} run --no-masking --)
            ${dropOpToken}
            # Preserve terminal streams so Claude starts its interactive UI.
            exec "''${launch[@]}" ${lib.getExe pkgs.claude-code} "$@"
          '';
        }).overrideAttrs
          { inherit (pkgs.claude-code) version; };
    };

    # Needed to allow for interactive runs when using CLAUDE_CODE_OAUTH_TOKEN.
    # An activation script, not a managed file, so .claude.json stays writable.
    home.activation.claudeOnboarding = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${
        lib.getExe (
          pkgs.writeShellApplication {
            name = "claude-onboarding";
            runtimeInputs = [
              pkgs.coreutils
              pkgs.jq
            ];
            text = builtins.readFile ./scripts/claude-onboarding.sh;
          }
        )
      } ${lib.escapeShellArgs ([ "${config.home.homeDirectory}/.claude.json" ] ++ cfg.trustedDirectories)}
    '';
  };
}
