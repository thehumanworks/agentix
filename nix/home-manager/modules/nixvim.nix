{ inputs }:
{
  config,
  lib,
  pkgs,
  ...
}:

{
  # Nixvim's own module, so its options exist even when this one is off.
  imports = [ inputs.nixvim.homeModules.nixvim ];

  options.agentix.nixvim.enable = lib.mkEnableOption "Neovim, configured with Nixvim" // {
    default = true;
  };

  config.programs.nixvim = lib.mkIf config.agentix.nixvim.enable {
    enable = true;
    defaultEditor = true;
    vimAlias = true;
    # No plugin here needs the Ruby provider, which costs about 40 MB.
    withRuby = false;
    # Build the editor from the same package set as everything else, so the
    # language servers on PATH and the ones Neovim starts are one copy.
    nixpkgs.useGlobalPackages = true;
    globals.mapleader = " ";

    opts = {
      number = true;
      signcolumn = "yes";
      expandtab = true;
      shiftwidth = 2;
      tabstop = 2;
      ignorecase = true;
      smartcase = true;
      splitright = true;
      splitbelow = true;
      updatetime = 250;
      termguicolors = true;
    };

    # Keep search available even outside the usual Home Manager shell.
    extraPackages = [
      pkgs.ripgrep
      pkgs.uv
      pkgs.shellcheck
    ];

    plugins = {
      blink-cmp = {
        enable = true;
        settings = {
          keymap.preset = "enter";
          completion.documentation.auto_show = true;
          signature.enabled = true;
        };
      };

      lsp = {
        enable = true;
        servers = {
          ty = {
            enable = true;
            # Resolve PEP 723 dependencies through uv, without hardcoded cache paths.
            # Preview feature; requires ty >= 0.0.76 and uv >= 0.12.3.
            extraOptions.init_options.experimental.useUv = "scripts";
          };
          ruff.enable = true;
          nil_ls.enable = true;
          bashls.enable = true; # Shell completion and ShellCheck diagnostics.
          lua_ls.enable = true;
          # Bun APIs are supplied by each project's @types/bun dependency.
          ts_ls.enable = true;
          html.enable = true;
          cssls.enable = true;
          jsonls.enable = true;
          yamlls.enable = true;
        };
        keymaps = {
          silent = true;
          lspBuf = {
            "gd" = "definition";
            "gr" = "references";
            "K" = "hover";
            "<leader>rn" = "rename";
            "<leader>ca" = "code_action";
          };
        };
      };

      telescope.enable = true;
      gitsigns.enable = true;
    };

    keymaps = [
      {
        mode = "n";
        key = "<leader>ff";
        action = "<cmd>Telescope find_files<CR>";
        options.desc = "Find files";
      }
      {
        mode = "n";
        key = "<leader>fg";
        action = "<cmd>Telescope live_grep<CR>";
        options.desc = "Search project text";
      }
      {
        mode = "n";
        key = "<leader>e";
        action = "<cmd>lua vim.diagnostic.open_float()<CR>";
        options.desc = "Show diagnostic";
      }
    ];
  };
}
