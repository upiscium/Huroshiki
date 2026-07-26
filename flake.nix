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
              chmod 0755 "$out/lib/huroshiki/huroshiki-launcher.sh"
              cp profiles.yaml "$out/share/huroshiki/profiles.yaml"
              cp completions/zsh/_packctl completions/zsh/_huroshiki "$out/share/zsh/site-functions/"
              makeWrapper ${python}/bin/python "$out/bin/huroshiki" \
                --add-flags "$out/lib/huroshiki/huroshiki.py" \
                --set HUROSHIKI_DATA_DIR "$out/share/huroshiki" \
                --prefix PATH : ${pkgs.lib.makeBinPath runtimeInputs}
              makeWrapper ${python}/bin/python "$out/bin/packctl" \
                --add-flags "$out/lib/huroshiki/packctl.py" \
                --set HUROSHIKI_DATA_DIR "$out/share/huroshiki" \
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
            ! ${huroshiki}/bin/packctl --root "$root" --help | grep -E 'migrate-template|[,{](use|current)[,}]'
            test "$(${huroshiki}/bin/packctl --root "$root" complete packs)" = example
            test "$(${huroshiki}/bin/packctl --root "$root" complete profiles example | grep -c '^base$')" = 1
            HUROSHIKI_PYTHON=${python}/bin/python \
              ${huroshiki}/lib/huroshiki/huroshiki-launcher.sh --root "$root" --help > /dev/null
            test -f ${huroshiki}/lib/huroshiki/huroshiki.tcss
            test -f ${huroshiki}/lib/huroshiki/huroshiki_core.py
            test -f ${huroshiki}/lib/huroshiki/overlay_policy.py
            test -f ${huroshiki}/lib/huroshiki/portable_paths.py
            test -f ${huroshiki}/lib/huroshiki/template_merge.py
            test -f ${huroshiki}/lib/huroshiki/huroshiki-launcher.sh
            test -x ${huroshiki}/lib/huroshiki/huroshiki-launcher.sh
            test -f ${huroshiki}/share/huroshiki/profiles.yaml
            test -f ${huroshiki}/share/zsh/site-functions/_packctl
            test -f ${huroshiki}/share/zsh/site-functions/_huroshiki
            test ! -e ${huroshiki}/share/zsh/site-functions/_just
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
              actionlint
              just
              tree
              ripgrep
              python
              huroshiki
            ];

            shellHook = ''
              echo "Minecraft modpack monorepo"
              echo "  packwiz: $(packwiz --version 2>/dev/null || echo available)"
              echo "  java:    $(java -version 2>&1 | head -n 1)"
              echo "  TUI:     huroshiki"
              echo "  dev tasks: just --list"
              echo "  zsh completion: installed in share/zsh/site-functions"
            '';
          };
        }
      );
    };
}
