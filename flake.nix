{
  description = "Multi-pack Packwiz management application";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      version = nixpkgs.lib.removeSuffix "\n" (
        builtins.readFile ./shared/scripts/VERSION
      );
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
            inherit version;
            src = ./shared;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            propagatedBuildInputs = [ python ] ++ runtimeInputs;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              mkdir -p "$out/lib/huroshiki" "$out/bin" "$out/share/huroshiki" "$out/share/zsh/site-functions"
              cp scripts/*.py scripts/*.tcss scripts/VERSION scripts/huroshiki-launcher.sh "$out/lib/huroshiki/"
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
          smoke = assert huroshiki.version == version; pkgs.runCommand "huroshiki-smoke" { } ''
            root="$TMPDIR/external-root"
            mkdir -p "$root/packs/example" "$root/templates"
            printf 'id: example\n' > "$root/packs/example/pack.yaml"
            test "$(${huroshiki}/bin/huroshiki --version)" = "huroshiki ${version}"
            test "$(${huroshiki}/bin/packctl --version)" = "packctl ${version}"
            test "$(${huroshiki}/bin/huroshiki --root "$root" --version)" = "huroshiki ${version}"
            test "$(${huroshiki}/bin/packctl --root "$root" --version)" = "packctl ${version}"
            ${huroshiki}/bin/huroshiki --root "$root" --help > /dev/null
            ${huroshiki}/bin/packctl --root "$root" --help > /dev/null
            ! ${huroshiki}/bin/packctl --root "$root" --help | grep -E 'migrate-template|[,{](use|current)[,}]'
            test "$(${huroshiki}/bin/packctl --root "$root" complete packs)" = example
            test "$(${huroshiki}/bin/packctl --root "$root" complete profiles example | grep -c '^base$')" = 1
            HUROSHIKI_PYTHON=${python}/bin/python \
              ${huroshiki}/lib/huroshiki/huroshiki-launcher.sh --root "$root" --help > /dev/null
            test -f ${huroshiki}/lib/huroshiki/huroshiki.tcss
            test -f ${huroshiki}/lib/huroshiki/VERSION
            test -f ${huroshiki}/lib/huroshiki/huroshiki.py
            test -f ${huroshiki}/lib/huroshiki/huroshiki_core.py
            test -f ${huroshiki}/lib/huroshiki/packctl.py
            test -f ${huroshiki}/lib/huroshiki/packwiz_pty.py
            test -f ${huroshiki}/lib/huroshiki/packwiz_parser.py
            test -f ${huroshiki}/lib/huroshiki/process_runner.py
            test -f ${huroshiki}/lib/huroshiki/overlay_policy.py
            test -f ${huroshiki}/lib/huroshiki/content_operations.py
            test -f ${huroshiki}/lib/huroshiki/content_workers.py
            test -f ${huroshiki}/lib/huroshiki/portable_paths.py
            test -f ${huroshiki}/lib/huroshiki/provider_lookup.py
            test -f ${huroshiki}/lib/huroshiki/template_import.py
            test -f ${huroshiki}/lib/huroshiki/template_merge.py
            test -f ${huroshiki}/lib/huroshiki/deploy_support.py
            test -f ${huroshiki}/lib/huroshiki/url_artifacts.py
            test -f ${huroshiki}/lib/huroshiki/project_locks.py
            test -f ${huroshiki}/lib/huroshiki/pack_migration.py
            test -f ${huroshiki}/lib/huroshiki/pack_tree_policy.py
            test -f ${huroshiki}/lib/huroshiki/packctl_errors.py
            test -f ${huroshiki}/lib/huroshiki/huroshiki_paths.py
            test -f ${huroshiki}/lib/huroshiki/huroshiki_version.py
            test -f ${huroshiki}/lib/huroshiki/huroshiki-launcher.sh
            test -x ${huroshiki}/lib/huroshiki/huroshiki-launcher.sh
            test -f ${huroshiki}/share/huroshiki/profiles.yaml
            test -f ${huroshiki}/share/zsh/site-functions/_packctl
            test -f ${huroshiki}/share/zsh/site-functions/_huroshiki
            grep -q "loader-version" ${huroshiki}/share/zsh/site-functions/_packctl
            grep -q "apply-template" ${huroshiki}/share/zsh/site-functions/_packctl
            grep -q "show-deployment" ${huroshiki}/share/zsh/site-functions/_packctl
            grep -q "set-deployment" ${huroshiki}/share/zsh/site-functions/_packctl
            grep -q "show-pack-url" ${huroshiki}/share/zsh/site-functions/_packctl
            grep -q "set-pack-url" ${huroshiki}/share/zsh/site-functions/_packctl
            test ! -e ${huroshiki}/share/zsh/site-functions/_just
            touch "$out"
          '';
          unitTests = pkgs.stdenvNoCC.mkDerivation {
            pname = "huroshiki-unit-tests";
            inherit version;
            src = ./.;
            nativeBuildInputs = [ python pkgs.git ] ++ runtimeInputs;
            dontBuild = true;
            doCheck = true;
            checkPhase = ''
              runHook preCheck
              patchShebangs shared/scripts/huroshiki-launcher.sh
              PYTHONPATH=shared/scripts ${python}/bin/python -m unittest discover -s tests -v
              runHook postCheck
            '';
            installPhase = ''
              touch "$out"
            '';
          };
        in
        { inherit pkgs python huroshiki smoke unitTests; };
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
        huroshiki-unit-tests = (perSystem system).unitTests;
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
