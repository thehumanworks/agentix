{ lib, pkgs, ... }:

{
  # On PATH for every editor and agent; Neovim and Claude use the same builds.
  home.packages = with pkgs; [
    ty
    ruff
    typescript-language-server
    bash-language-server
    lua-language-server
    vscode-langservers-extracted # HTML, CSS, JSON, ESLint
    yaml-language-server
    nixd
    nil
  ];

  # Gives Claude go-to-definition, references, and diagnostics after each edit.
  # A server starts the first time Claude opens a matching file.
  programs.claude-code.lspServers = {
    python = {
      command = lib.getExe pkgs.ty;
      args = [ "server" ];
      extensionToLanguage = {
        ".py" = "python";
        ".pyi" = "python";
      };
    };
    typescript = {
      command = lib.getExe pkgs.typescript-language-server;
      args = [ "--stdio" ];
      extensionToLanguage = {
        ".ts" = "typescript";
        ".tsx" = "typescriptreact";
        ".mts" = "typescript";
        ".cts" = "typescript";
        ".js" = "javascript";
        ".jsx" = "javascriptreact";
        ".mjs" = "javascript";
        ".cjs" = "javascript";
      };
    };
    bash = {
      command = lib.getExe pkgs.bash-language-server;
      args = [ "start" ];
      extensionToLanguage = {
        ".sh" = "shellscript";
        ".bash" = "shellscript";
      };
    };
    nix = {
      command = lib.getExe pkgs.nixd;
      extensionToLanguage.".nix" = "nix";
    };
  };
}
