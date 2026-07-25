{
  description = "Multi-pack Packwiz management application";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      perSystem = system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3.withPackages (ps: with ps; [
            pyyaml
            textual
            tomlkit
          ]);
          runtimeInputs = with pkgs; [
            packwiz
            just
            jdk21_headless
            rsync
            openssh
          ];
          huroshiki = pkgs.stdenvNoCC.mkDerivation {
            pname = "huroshiki";
            version = "0.1.0";
            src = ./shared;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            propagatedBuildInputs = [ python ] ++ runtimeInputs;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              mkdir -p "$out/lib/huroshiki" "$out/bin" "$out/share/huroshiki" "$out/share/zsh/site-functions"
              cp scripts/*.py scripts/*.tcss scripts/huroshiki-launcher.sh "$out/lib/huroshiki/"
              cp profiles.yaml "$out/share/huroshiki/profiles.yaml"
              cp completions/zsh/_just "$out/share/zsh/site-functions/_just"
              makeWrapper ${python}/bin/python "$out/bin/huroshiki" \
                --add-flags "$out/lib/huroshiki/huroshiki.py" \
                --prefix PATH : ${pkgs.lib.makeBinPath runtimeInputs}
              makeWrapper ${python}/bin/python "$out/bin/packctl" \
                --add-flags "$out/lib/huroshiki/packctl.py" \
                --prefix PATH : ${pkgs.lib.makeBinPath runtimeInputs}
              runHook postInstall
            '';
          };
          smoke = pkgs.runCommand "huroshiki-smoke" { } ''
            root="$TMPDIR/external-root"
            mkdir -p "$root/packs/example" "$root/templates"
            printf 'id: example\n' > "$root/packs/example/pack.yaml"
            ${huroshiki}/bin/huroshiki --root "$root" --help > /dev/null
            ${huroshiki}/bin/packctl --root "$root" --help > /dev/null
            test "$(${huroshiki}/bin/packctl --root "$root" complete packs)" = example
            test -f ${huroshiki}/lib/huroshiki/huroshiki.tcss
            test -f ${huroshiki}/lib/huroshiki/huroshiki_core.py
            test -f ${huroshiki}/lib/huroshiki/huroshiki-launcher.sh
            test -f ${huroshiki}/share/huroshiki/profiles.yaml
            test -f ${huroshiki}/share/zsh/site-functions/_just
            touch "$out"
          '';
        in
        { inherit pkgs python huroshiki smoke; };
    in
    {
      packages = forAllSystems (system: {
        huroshiki = (perSystem system).huroshiki;
        default = (perSystem system).huroshiki;
      });

      apps = forAllSystems (system: {
        huroshiki = {
          type = "app";
          program = "${(perSystem system).huroshiki}/bin/huroshiki";
          meta.description = "Manage multiple Packwiz projects";
        };
        default = {
          type = "app";
          program = "${(perSystem system).huroshiki}/bin/huroshiki";
          meta.description = "Manage multiple Packwiz projects";
        };
      });

      checks = forAllSystems (system: {
        huroshiki-package = (perSystem system).huroshiki;
        huroshiki-smoke = (perSystem system).smoke;
      });

      devShells = forAllSystems (
        system:
        let
          inherit (perSystem system) pkgs python huroshiki;
        in
        {
          default = pkgs.mkShell {
            inputsFrom = [ huroshiki ];
            packages = with pkgs; [
              git
              tree
              ripgrep
              python
              huroshiki
            ];

            shellHook = ''
              completion_dir="${huroshiki}/share/zsh/site-functions"
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
