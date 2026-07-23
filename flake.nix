{
  description = "Multi-pack Packwiz development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3.withPackages (ps: with ps; [
            pyyaml
            textual
            tomlkit
          ]);
          huroshiki = pkgs.writeShellApplication {
            name = "huroshiki";
            runtimeInputs = [ python ];
            text = builtins.readFile ./shared/scripts/huroshiki-launcher.sh;
          };
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              packwiz
              just
              git
              rsync
              openssh
              curl
              jq
              tree
              ripgrep
              jdk21_headless
              python
              huroshiki
            ];

            shellHook = ''
              completion_dir="$PWD/shared/completions/zsh"
              case ":''${FPATH-}:" in
                *":$completion_dir:"*) ;;
                *) export FPATH="$completion_dir''${FPATH:+:$FPATH}" ;;
              esac

              echo "Minecraft modpack monorepo"
              echo "  packwiz: $(packwiz --version 2>/dev/null || echo available)"
              echo "  java:    $(java -version 2>&1 | head -n 1)"
              echo "  TUI:     huroshiki"
              echo "  recipes: just --list"
              echo "  zsh completion: packs, profiles, metadata and side"
            '';
          };
        }
      );
    };
}
