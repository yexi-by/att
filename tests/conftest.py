"""测试夹具：构造最小可用的 RPG Maker MV/MZ 游戏目录。"""

import json
from pathlib import Path

import pytest

from app.rmmz.text_rules import JsonValue
from app.runtime_paths import APP_HOME_ENV_NAME

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


@pytest.fixture
def app_home_with_example_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """创建带示例配置的临时应用目录，避免测试依赖开发机私有配置。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    setting_text = EXAMPLE_SETTING_PATH.read_text(encoding="utf-8").replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{(ROOT / "prompts" / "text_translation_ja_to_zh_system.md").as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv(APP_HOME_ENV_NAME, str(app_home))
    return app_home


def write_json(path: Path, value: JsonValue) -> None:
    """以 UTF-8 写入 JSON 文件，保持 fixture 内容易读。"""
    _ = path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_plugin_source_stubs(js_dir: Path, plugin_names: list[str]) -> None:
    """为已启用插件写入无可见文本的源码 stub。"""
    plugin_source_dir = js_dir / "plugins"
    plugin_source_dir.mkdir()
    for plugin_name in plugin_names:
        _ = (plugin_source_dir / f"{plugin_name}.js").write_text(
            "(() => {})();\n",
            encoding="utf-8",
        )


def write_complete_standard_data_files(data_dir: Path, *, map_ids: list[int]) -> None:
    """补齐测试游戏必须存在的 RPG Maker 标准 data 文件。"""
    supplemental_files: dict[str, JsonValue] = {
        "Actors.json": [None, {"id": 1, "name": "", "note": "", "nickname": "", "profile": ""}],
        "Animations.json": [None, {"id": 1, "name": ""}],
        "Armors.json": [None, {"id": 1, "name": "", "note": "", "description": ""}],
        "Classes.json": [None, {"id": 1, "name": "", "note": ""}],
        "Enemies.json": [None, {"id": 1, "name": "", "note": ""}],
        "Items.json": [None, {"id": 1, "name": "", "note": "", "description": ""}],
        "Skills.json": [None, {"id": 1, "name": "", "note": "", "description": "", "message1": ""}],
        "States.json": [None, {"id": 1, "name": "", "note": ""}],
        "Tilesets.json": [None, {"id": 1, "name": "", "note": ""}],
        "Weapons.json": [None, {"id": 1, "name": "", "note": "", "description": ""}],
        "MapInfos.json": [
            None,
            *(
                {
                    "id": map_id,
                    "expanded": False,
                    "name": "",
                    "order": index,
                    "parentId": 0,
                    "scrollX": 0,
                    "scrollY": 0,
                }
                for index, map_id in enumerate(map_ids, start=1)
            ),
        ],
    }
    for file_name, value in supplemental_files.items():
        path = data_dir / file_name
        if path.exists():
            continue
        write_json(path, value)


@pytest.fixture
def minimal_game_dir(tmp_path: Path) -> Path:
    """创建只包含核心流程所需文件的最小 MZ 游戏目录。"""
    game_root = tmp_path / "mini-game"
    data_dir = game_root / "data"
    js_dir = game_root / "js"
    data_dir.mkdir(parents=True)
    js_dir.mkdir(parents=True)

    write_json(game_root / "package.json", {"window": {"title": "テストゲーム"}})
    write_json(
        data_dir / "System.json",
        {
            "gameTitle": "テストゲーム",
            "terms": {
                "basic": ["", "HP"],
                "commands": ["", "戦う"],
                "params": ["攻撃"],
                "messages": {"alwaysDash": "常時ダッシュ"},
            },
            "elements": ["", "炎"],
            "skillTypes": ["", "魔法"],
            "weaponTypes": ["", "剣"],
            "armorTypes": ["", "盾"],
            "equipTypes": ["", "武器"],
        },
    )
    write_json(
        data_dir / "CommonEvents.json",
        [
            None,
            {
                "id": 1,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2, "アリス"]},
                    {"code": 401, "parameters": ["こんにちは"]},
                    {"code": 102, "parameters": [["はい", "いいえ"], 0, 0, 2, 0]},
                    {"code": 405, "parameters": ["スクロール本文"]},
                    {
                        "code": 357,
                        "parameters": [
                            "TestPlugin",
                            "Show",
                            0,
                            {"message": "プラグイン台詞", "file": "Actor1.png"},
                        ],
                    },
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 2,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2, "案内人"]},
                    {"code": 401, "parameters": [r"\F[GuideA]テスト一行目です。\!"]},
                    {"code": 401, "parameters": [r"\C[4]重要語\C[0]を含む二行目です。"]},
                    {"code": 401, "parameters": ["Plain English helper line"]},
                    {"code": 102, "parameters": [["第一選択", "English Choice"], 0, 0, 2, 0]},
                    {"code": 405, "parameters": ["スクロール一行目"]},
                    {"code": 405, "parameters": ["スクロール二行目"]},
                    {"code": 405, "parameters": [""]},
                    {"code": 405, "parameters": [r"\F[ScrollFace]別スクロール"]},
                    {
                        "code": 357,
                        "parameters": [
                            "ComplexPlugin",
                            "ShowWindow",
                            0,
                            {
                                "window": {
                                    "title": "複雑タイトル",
                                    "body": "複雑本文",
                                },
                                "choices": ["第一項目", "第二項目"],
                                "file": "img/pictures/Window.png",
                            },
                        ],
                    },
                    {"code": 0, "parameters": []},
                ],
            },
        ],
    )
    write_json(
        data_dir / "Troops.json",
        [
            None,
            {
                "id": 1,
                "pages": [
                    {
                        "list": [
                            {"code": 101, "parameters": [0, 0, 0, 2, "敵"]},
                            {"code": 401, "parameters": ["敵の台詞"]},
                            {"code": 0, "parameters": []},
                        ]
                    }
                ],
            },
        ],
    )
    write_json(
        data_dir / "Map001.json",
        {
            "displayName": "始まりの町",
            "note": "",
            "events": [
                None,
                {
                    "id": 1,
                    "name": "村人",
                    "note": "",
                    "pages": [
                        {
                            "list": [
                                {"code": 101, "parameters": [0, 0, 0, 2, "村人"]},
                                {"code": 401, "parameters": ["マップこんにちは"]},
                                {"code": 0, "parameters": []},
                            ]
                        }
                    ],
                },
                {
                    "id": 2,
                    "name": "案内イベント",
                    "note": "",
                    "pages": [
                        {
                            "list": [
                                {"code": 101, "parameters": [0, 0, 0, 2, "案内人"]},
                                {"code": 401, "parameters": [r"\F[MapFace]マップ案内です。"]},
                                {"code": 401, "parameters": ["重要地点へ進みます。"]},
                                {"code": 102, "parameters": [["進む", "戻る"], 0, 0, 2, 0]},
                                {"code": 0, "parameters": []},
                            ]
                        }
                    ],
                },
            ],
        },
    )
    write_json(
        data_dir / "Map002.json",
        {
            "displayName": "第二テスト地点",
            "note": "",
            "events": [
                None,
                {
                    "id": 1,
                    "name": "説明役",
                    "note": "",
                    "pages": [
                        {
                            "list": [
                                {"code": 101, "parameters": [0, 0, 0, 2, "説明役"]},
                                {"code": 401, "parameters": ["別マップの本文です。"]},
                                {"code": 0, "parameters": []},
                            ]
                        }
                    ],
                },
            ],
        },
    )
    write_json(
        data_dir / "Actors.json",
        [
            None,
            {
                "id": 1,
                "name": "勇者",
                "note": "",
                "nickname": "ニック",
                "profile": "プロフィール",
            },
        ],
    )
    write_json(
        data_dir / "Items.json",
        [
            None,
            {
                "id": 1,
                "name": "回復薬",
                "note": "",
                "description": "体力を回復する。",
            },
        ],
    )
    write_json(
        data_dir / "Skills.json",
        [
            None,
            {
                "id": 1,
                "name": "火の術",
                "note": "",
                "description": "炎で攻撃する。",
                "message1": "は火の術を唱えた！",
            },
        ],
    )
    write_json(data_dir / "UnknownPluginData.json", [{"id": 1, "name": "これは無視される"}])
    write_complete_standard_data_files(data_dir, map_ids=[1, 2])

    plugins: list[JsonValue] = [
        {
            "name": "TestPlugin",
            "status": True,
            "description": "テスト説明",
            "parameters": {
                "Message": "プラグイン本文",
                "Nested": json.dumps({"text": "入れ子本文", "file": "Actor1.png"}, ensure_ascii=False),
                "List": json.dumps(
                    [
                        {"text": "配列本文", "file": "Window.png"},
                        {"text": "二つ目の本文", "enabled": "true"},
                    ],
                    ensure_ascii=False,
                ),
                "File": "img/pictures/Actor1.png",
                "Count": "123",
            },
        },
        {
            "name": "ComplexPlugin",
            "status": True,
            "description": "複雑テスト",
            "parameters": {
                "Window": json.dumps(
                    {
                        "title": "ウィンドウ見出し",
                        "body": "ウィンドウ本文",
                        "font": "GameFont",
                    },
                    ensure_ascii=False,
                ),
                "Rows": json.dumps(
                    [
                        {"label": "一行目", "path": "img/system/Icon.png"},
                        {"label": "二行目", "value": "auto"},
                    ],
                    ensure_ascii=False,
                ),
            },
        },
    ]
    plugins_text = f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n"
    _ = (js_dir / "plugins.js").write_text(plugins_text, encoding="utf-8")
    write_plugin_source_stubs(js_dir, ["TestPlugin", "ComplexPlugin"])
    return game_root


@pytest.fixture
def minimal_mv_game_dir(tmp_path: Path) -> Path:
    """创建外层目录含可执行文件、真实数据位于 www 的最小 MV 游戏目录。"""
    game_root = tmp_path / "mini-mv-game"
    content_root = game_root / "www"
    data_dir = content_root / "data"
    js_dir = content_root / "js"
    data_dir.mkdir(parents=True)
    js_dir.mkdir(parents=True)

    _ = (game_root / "Game.exe").write_bytes(b"")
    write_json(game_root / "package.json", {"window": {"title": ""}, "main": "www/index.html"})
    _ = (js_dir / "rpg_core.js").write_text(
        "Utils.RPGMAKER_NAME = 'MV';\nUtils.RPGMAKER_VERSION = \"1.6.1\";\n",
        encoding="utf-8",
    )
    write_json(
        data_dir / "System.json",
        {
            "gameTitle": "MVテストゲーム",
            "terms": {
                "basic": ["", "HP"],
                "commands": ["", "戦う"],
                "params": ["攻撃"],
                "messages": {"alwaysDash": "常時ダッシュ"},
            },
            "elements": ["", "炎"],
            "skillTypes": ["", "魔法"],
            "weaponTypes": ["", "剣"],
            "armorTypes": ["", "盾"],
            "equipTypes": ["", "武器"],
        },
    )
    write_json(
        data_dir / "CommonEvents.json",
        [
            None,
            {
                "id": 1,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["MVの本文です"]},
                    {
                        "code": 356,
                        "parameters": [
                            "ShowMvText text:MVプラグイン本文 name:案内人",
                        ],
                    },
                    {"code": 0, "parameters": []},
                ],
            },
        ],
    )
    write_json(
        data_dir / "Troops.json",
        [
            None,
            {
                "id": 1,
                "pages": [
                    {
                        "list": [
                            {"code": 101, "parameters": [0, 0, 0, 2]},
                            {"code": 401, "parameters": ["敵の本文"]},
                            {"code": 0, "parameters": []},
                        ]
                    }
                ],
            },
        ],
    )
    write_json(
        data_dir / "Map001.json",
        {
            "displayName": "MVの町",
            "note": "",
            "events": [
                None,
                {
                    "id": 1,
                    "name": "案内イベント",
                    "note": "",
                    "pages": [
                        {
                            "list": [
                                {"code": 101, "parameters": [0, 0, 0, 2]},
                                {"code": 401, "parameters": ["マップ本文"]},
                                {"code": 0, "parameters": []},
                            ]
                        }
                    ],
                },
            ],
        },
    )
    write_json(
        data_dir / "Actors.json",
        [
            None,
            {
                "id": 1,
                "name": "MV勇者",
                "note": "",
                "nickname": "MVニック",
                "profile": "MVプロフィール",
            },
        ],
    )
    write_complete_standard_data_files(data_dir, map_ids=[1])

    plugins: list[JsonValue] = [
        {
            "name": "MvPlugin",
            "status": True,
            "description": "MVテスト説明",
            "parameters": {"Message": "MVプラグイン設定本文"},
        }
    ]
    plugins_text = f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n"
    _ = (js_dir / "plugins.js").write_text(plugins_text, encoding="utf-8")
    write_plugin_source_stubs(js_dir, ["MvPlugin"])
    return game_root


@pytest.fixture
def minimal_english_game_dir(tmp_path: Path) -> Path:
    """创建只含英文玩家可见文本的最小 MZ 游戏目录。"""
    game_root = tmp_path / "english-mini-game"
    data_dir = game_root / "data"
    js_dir = game_root / "js"
    data_dir.mkdir(parents=True)
    js_dir.mkdir(parents=True)

    write_json(game_root / "package.json", {"window": {"title": "English Fixture Game"}})
    write_json(
        data_dir / "System.json",
        {
            "gameTitle": "English Fixture Game",
            "terms": {
                "basic": ["", "HP", "MP"],
                "commands": ["", "Fight", "Escape"],
                "params": ["Attack"],
                "messages": {"alwaysDash": "Always Dash"},
            },
            "elements": ["", "Fire"],
            "skillTypes": ["", "Magic"],
            "weaponTypes": ["", "Sword"],
            "armorTypes": ["", "Shield"],
            "equipTypes": ["", "Weapon"],
        },
    )
    write_json(
        data_dir / "CommonEvents.json",
        [
            None,
            {
                "id": 1,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2, "Guide"]},
                    {"code": 401, "parameters": ["Are you really going in there?"]},
                    {"code": 102, "parameters": [["Open the door", "Leave"], 0, 0, 2, 0]},
                    {
                        "code": 357,
                        "parameters": [
                            "VisiblePlugin",
                            "Show",
                            0,
                            {
                                "message": "Plugin visible line",
                                "file": "img/pictures/Window.png",
                                "enabled": "true",
                            },
                        ],
                    },
                    {"code": 0, "parameters": []},
                ],
            },
        ],
    )
    write_json(
        data_dir / "Map001.json",
        {
            "displayName": "Old Gate",
            "note": "<Flavor:Ancient warning>",
            "events": [
                None,
                {
                    "id": 1,
                    "name": "Gatekeeper",
                    "note": "",
                    "pages": [
                        {
                            "list": [
                                {"code": 101, "parameters": [0, 0, 0, 2, "Gatekeeper"]},
                                {"code": 401, "parameters": ["The bridge is closed tonight."]},
                                {"code": 0, "parameters": []},
                            ]
                        }
                    ],
                },
            ],
        },
    )
    write_json(
        data_dir / "Troops.json",
        [
            None,
            {
                "id": 1,
                "pages": [
                    {
                        "list": [
                            {"code": 101, "parameters": [0, 0, 0, 2, "Enemy"]},
                            {"code": 401, "parameters": ["You cannot pass."]},
                            {"code": 0, "parameters": []},
                        ]
                    }
                ],
            },
        ],
    )
    write_json(
        data_dir / "Actors.json",
        [
            None,
            {
                "id": 1,
                "name": "Mira",
                "note": "<Profile:Village guard>",
                "nickname": "Rookie",
                "profile": "A guard who knows every alley.",
            },
        ],
    )
    write_json(
        data_dir / "Skills.json",
        [
            None,
            {
                "id": 1,
                "name": "Flame",
                "note": "",
                "description": "Deals fire damage to one enemy.",
                "message1": " casts Flame!",
                "damage": {"formula": "a.mat * 4 - b.mdf * 2"},
            },
        ],
    )
    write_json(
        data_dir / "Items.json",
        [
            None,
            {
                "id": 1,
                "name": "Potion",
                "note": "",
                "description": "Restores 50 HP.",
            },
        ],
    )
    write_complete_standard_data_files(data_dir, map_ids=[1])

    plugins: list[JsonValue] = [
        {
            "name": "VisiblePlugin",
            "status": True,
            "description": "Plugin test",
            "parameters": {
                "Message": "Welcome to the old gate.",
                "Title": "Gate Menu",
                "Image": "img/pictures/Gate.png",
                "Formula": "a.hpRate() >= 0.5",
                "Enabled": "true",
            },
        }
    ]
    plugins_text = f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n"
    _ = (js_dir / "plugins.js").write_text(plugins_text, encoding="utf-8")
    write_plugin_source_stubs(js_dir, ["VisiblePlugin"])
    return game_root
