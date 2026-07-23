# Minecraft Packwiz project monorepo

複数のMODPACKと、再利用可能なMODリストテンプレートを同じリポジトリで管理します。
管理UIはTextual製TUIの`huroshiki`です。

## 基本モデル

このリポジトリには2種類のプロジェクトがあります。

- `MODPACK`: Packwizの`pack.toml`、`index.toml`、`*.pw.toml`を持つ実体
- `TEMPLATE`: Minecraftバージョン、ローダ、MODのprovider ID、sideを持つ作成レシピ

テンプレート自身はPackwizプロジェクトではありません。MODのバージョンやローダバージョンを固定せず、
新規MODPACKを作る際に、対象MODPACKのMinecraft・ローダ・ローダバージョンに対してPackwizで
導入可能な最新版を解決します。

## ディレクトリ構成

```text
minecraft-modpacks-monorepo/
├── flake.nix
├── .envrc
├── Justfile
├── shared/
│   ├── profiles.yaml
│   ├── completions/zsh/_just
│   └── scripts/
│       ├── packctl.py
│       ├── huroshiki.py
│       ├── huroshiki_core.py
│       ├── packwiz_pty.py
│       └── packwiz_parser.py
├── templates/
│   └── base/
│       └── template.yaml
├── packs/
│   └── the-crimson-infection/
│       ├── pack.yaml
│       ├── pack.local.yaml.example
│       ├── profiles.yaml
│       ├── source/
│       │   ├── pack.toml
│       │   ├── index.toml
│       │   └── mods/*.pw.toml
│       ├── content/
│       │   ├── common/
│       │   ├── client/
│       │   └── server/
│       └── dist/
└── deploy/packwiz-web/
```

## テンプレート形式

```yaml
id: industrial-base
display_name: Industrial Base
enabled: true
minecraft: 1.21.1
loader: neoforge
reference_loader_version: 21.1.234
mods:
  - name: Create
    provider: modrinth
    project_id: LNytGWDc
    side: both
  - name: JEI
    provider: curseforge
    project_id: "238222"
    side: client
```

`reference_loader_version`はテンプレートのMODを検索・登録するときに使う基準値です。
テンプレート候補の絞り込みには使用しません。

URL providerのJARはデフォルトで256 MiBまでダウンロードできます。packまたはtemplateの
`pack.yaml` / `template.yaml`、あるいはgit管理外の`pack.local.yaml` /
`template.local.yaml`でバイト単位の上限を上書きできます。

```yaml
url_max_jar_size_bytes: 536870912
```

以前のPackwizプロジェクト型テンプレートは、次で`source/*.pw.toml`からMODリストを抽出できます。

```bash
just migrate-template base
```

移行後も旧`source/`は自動削除しません。内容を確認してから手動で削除します。

## 開発環境

```bash
direnv allow
```

または：

```bash
nix develop
```

## huroshiki

```bash
huroshiki
```

直接開く場合：

```bash
huroshiki --pack the-crimson-infection
huroshiki --template base
```

### メインメニュー

```text
Tab       検索窓と一覧のフォーカス切り替え
Enter/p   選択プロジェクトを開く
j / k     一覧移動
n         空のMODPACKまたはTEMPLATEを新規作成
f         テンプレートからMODPACKを作成
d         選択プロジェクトの削除確認
q         huroshiki終了
r         Trash・ログ・transaction状態の管理
```

`f`では先に新規MODPACKの以下を入力します。

```text
Project ID
Display name
Minecraft version
Loader
Loader version
```

その後、**Minecraftバージョンとローダ種類が一致するテンプレートだけ**を候補表示します。
テンプレートの`reference_loader_version`と新規MODPACKのローダバージョンが違っていても候補から除外しません。

## テンプレートプロジェクト

テンプレートを開くと、MODリストを通常のMOD一覧に近い操作で編集できます。

```text
i         Packwiz検索からMODをテンプレートへ登録
l         登録済みMOD一覧
c / s     client/server指定を切り替え
b         client/serverを両方有効化
Space     削除対象チェック
d         選択済みMODを削除
```

インストール画面では一時的なPackwiz resolverプロジェクトを作成し、PTY経由で検索候補を
huroshiki内へ表示します。選択後に生成された`.pw.toml`からprovider、project ID、名前、sideを
抽出し、`template.yaml`の`mods`へ保存します。一時Packwizプロジェクト自体は残しません。

テンプレートの中央メニュー：

```text
create MODPACK
validate
```

`create MODPACK`ではそのテンプレートを直接選択した状態で新規MODPACKを作成できます。

## テンプレートからの作成

作成処理はテンプレートの各MODをproject IDで順番に導入します。

```text
新規Packwizプロジェクト作成
→ MOD 1を導入
→ MOD 2を導入
→ ...
→ packwiz refresh
→ 成功・失敗一覧を表示
```

あるMODに対象ローダバージョン向けファイルがない場合や、その他の理由でPackwiz追加に失敗した場合も、
MODPACK全体の作成は中止しません。そのMODだけをロールバックして残りのMODを続行し、最後に警告として
一覧表示します。

```text
Installed 18 MOD(s).
Could not install 2 MOD(s):
- Example A (modrinth:xxxx): no compatible version found
- Example B (curseforge:1234): no compatible files for loader
```

依存MODとして先に導入済みになったテンプレート項目は再ダウンロードせず、テンプレート側のsideだけを反映します。

## MODPACKプロジェクト

```text
i         MODインストール
l         導入済みMOD一覧
u         全MOD一括アップデート
t         content/common|client|serverの編集
Esc       メインメニュー
```

中央メニュー：

```text
build
publish
deploy
restart
```

テンプレートは作成時にMODをコピーするレシピであり、ビルド時の継承・重ね合わせは行いません。
作成後のMODPACKは独立したPackwizプロジェクトになります。
将来の任意比較・差分適用については、[Template-to-pack comparison design](docs/template-comparison.md)を参照してください。

## セルフホストURL MOD

インストールモードで`Ctrl+t`を押し、providerを`URL`へ切り替えます。検索欄には、
Packwiz Installerを実行するクライアントから直接取得できる自作MODの公開`.jar` URLを入力します。

```text
Provider: URL
https://minecraft.example.jp/mods/my-private-mod/1.2.0/my-private-mod-1.2.0.jar
```

huroshikiはJARを取得してSHA-256を計算し、NeoForge／Forge／Fabric／Quiltの
メタデータからMOD IDと表示名を読み取ります。生成されるメタデータは
`mods/<mod-id>.pw.toml`です。同じMOD IDを持つ新しいURLを登録すると同じメタデータを
ステージ上で置き換えるため、URLやJAR名がバージョンごとに変わっても旧版と新版が
二重登録されません。

テンプレートにも`provider: url`として保持できます。MODPACK作成時にURLからJARを再取得し、
登録時と同じMOD IDであることを確認してから導入します。

## MODインストール

```text
Tab       検索窓と結果一覧のフォーカス切り替え
Ctrl+t    Modrinth / CurseForge / URL切り替え
Enter     検索、検索結果選択、ステージ内容確認
q         表示中の検索結果を破棄し、Packwiz検索をキャンセル
j / k     一覧移動
c / s     client/serverを切り替え
b         client/serverを両方有効化
d         Staged changesで選択中のMODをステージから削除
l         導入済みMOD一覧
p         プロジェクトモード
Esc       メインメニュー
```

C/S表示：

```text
C +  S +
C +  S -
C -  S +
```

## Just recipes

```bash
just huroshiki
just huroshiki-for the-crimson-infection
just huroshiki-template base

just new \
  create-aeronautics \
  "Create Aeronautics" \
  1.21.1 \
  neoforge \
  21.1.234

just new-template \
  industrial-base \
  "Industrial Base" \
  1.21.1 \
  neoforge \
  21.1.234

just template-projects
just validate-template base
just migrate-template legacy-template
just test-huroshiki
```

テンプレートからのMODPACK作成は、候補・警告を確認できるhuroshikiから実行します。

## Trashと状態保持

プロジェクト削除は`packs/`または`templates/`から同じfilesystem上の
`.huroshiki/trash/<timestamp>-<kind>-<id>`へのrenameです。`pack.local.yaml`、
Git管理外ファイル、作業中ファイルを含むディレクトリ全体を保持します。復元先に同じIDが
存在する場合は上書きせず停止します。TUIではメイン画面の`r`から一覧、復元、個別purge、
状態cleanupのdry-runと適用を操作できます。

```bash
just trash-list
just trash-restore 20260723-120000-000000-pack-example
just trash-purge 20260723-120000-000000-pack-example
```

`.huroshiki`の状態cleanupは、Packwizログ、完了済みtransaction残骸、異常終了した
transaction候補、Trash、active transaction/lockを分類し、件数とbyte数を表示します。
active transactionとlockは削除対象になりません。既定保持期間はログ30日、完了済み・
異常終了transaction 7日、Trash 30日です。起動時の自動purgeは行いません。
`clean-huroshiki-state`は常にdry-runで、削除には`--apply`または
`purge-huroshiki-state`が必要です。

```bash
just clean-huroshiki-state
just clean-huroshiki-state -- --older-than 14 --keep 5 --project pack:example
just purge-huroshiki-state -- --older-than 14 --project pack:example
python shared/scripts/packctl.py trash-purge --project pack:example
```

`--older-than DAYS`は全分類の既定期間を上書きし、`--keep COUNT`は分類ごとに新しい項目を
指定件数保持します。`--project`は`pack:<id>`または`template:<id>`形式です。

## 配布

```bash
just build-for the-crimson-infection
just deploy-for the-crimson-infection
just publish-for the-crimson-infection
```

生成先：

```text
packs/<pack>/dist/client/
packs/<pack>/dist/server/
```

## テスト

```bash
just test-huroshiki
```

テスト対象：

- Packwiz番号メニュー解析
- ANSI・改行・プログレス表示正規化
- PTY番号選択と確認応答
- テンプレートMODリストの読み書き、side変更、削除
- Minecraft・ローダ一致とローダバージョン非依存の候補判定
- テンプレート追加結果のmanifest変換
- 一部MOD失敗時の継続導入と警告収集
- MODPACKのclient/serverビルド
