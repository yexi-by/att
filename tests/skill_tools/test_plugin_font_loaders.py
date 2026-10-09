from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "translate-with-att" / "scripts"
SELECTED_FONT = ROOT / "skills" / "translate-with-att" / "assets" / "fonts" / "NotoSansCJKsc-Regular.otf"
sys.path.insert(0, str(ROOT / "skills" / "_shared"))
sys.path.insert(0, str(SCRIPTS))

from att_toolbox.font_references import FontPlan, build_font_plan
from att_toolbox.rpg import parse_plugins


class PluginFontLoaderTests(unittest.TestCase):
    def _game(
        self,
        root: Path,
        *,
        filenames: str = " Popup-Italic.ttf, Decorative.TTF , Fancy Script.ttf ",
        families: str = " Popup-Italic, 装饰 , Fancy Script ",
        loader: str = "YEP_LoadCustomFonts",
        active: bool = True,
    ) -> Path:
        game = root / "game"
        (game / "data").mkdir(parents=True)
        (game / "js" / "plugins").mkdir(parents=True)
        (game / "fonts").mkdir()
        (game / "data" / "System.json").write_text("{}", encoding="utf-8")
        (game / "js" / "rpg_core.js").write_text("// MV\n", encoding="utf-8")
        (game / "package.json").write_text('{"main":"index.html"}', encoding="utf-8")
        (game / "index.html").write_text("<html></html>", encoding="utf-8")
        (game / "js" / "plugins" / f"{loader}.js").write_text(
            f"var params = PluginManager.parameters('{loader}');\n", encoding="utf-8"
        )
        (game / "js" / "plugins" / "Popup.js").write_text("// font consumer\n", encoding="utf-8")
        for name in ("Popup-Italic.ttf", "Decorative.TTF", "Fancy Script.ttf"):
            (game / "fonts" / name).write_bytes(name.encode("utf-8"))
        plugins = [
            {
                "name": loader,
                "status": active,
                "parameters": {"Font Filenames": filenames, "Font Families": families},
            },
            {
                "name": "Popup",
                "status": True,
                "parameters": {"Font": "Popup-Italic", "Label": "Popup-Italic"},
            },
        ]
        text = "// plugins\nvar $plugins = " + json.dumps(plugins, indent=2) + ";\n"
        # 一个文件名使用 JSON escape；family 的 Unicode escape 和列表排版应保持原字节。
        text = text.replace("Decorative.TTF", r"Decorative\u002ETTF")
        (game / "js" / "plugins.js").write_text(text, encoding="utf-8")
        return game

    def _plan(self, game: Path, *, selected_font: Path = SELECTED_FONT) -> FontPlan:
        return build_font_plan(game_root=game, content_root=game, selected_font=selected_font)

    def _updated_plugins(self, game: Path, plan: FontPlan) -> str:
        mutation = next((m for m in plan.mutations if m.relative_path == "js/plugins.js"), None)
        return (
            mutation.replacement.decode("utf-8")
            if mutation is not None
            else (game / "js/plugins.js").read_text(encoding="utf-8")
        )

    def test_cli_replaces_paired_assets_preserves_families_and_restores_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = self._game(root)
            before = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
            coverage = root / "coverage.txt"
            coverage.write_text("战斗伤害暴击", encoding="utf-8")
            tool = [sys.executable, "-B", str(SCRIPTS / "manage_rpg_maker_fonts.py")]
            report_path = root / "apply.json"
            state = root / "font-state"
            result = subprocess.run(
                [
                    *tool,
                    "apply",
                    "--game",
                    str(game),
                    "--font",
                    "noto-sans-sc",
                    "--coverage-text",
                    str(coverage),
                    "--state",
                    str(state),
                    "--output",
                    str(report_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            updated_text = (game / "js/plugins.js").read_text(encoding="utf-8")
            plugins = parse_plugins(updated_text, "plugins.js")
            self.assertEqual(
                plugins[0].parameters["Font Filenames"],
                " NotoSansCJKsc-Regular.otf, NotoSansCJKsc-Regular.otf , NotoSansCJKsc-Regular.otf ",
            )
            self.assertEqual(plugins[0].parameters["Font Families"], " Popup-Italic, 装饰 , Fancy Script ")
            self.assertEqual(plugins[1].parameters, {"Font": "Popup-Italic", "Label": "Popup-Italic"})
            old_family_line = next(
                line
                for line in before[Path("js/plugins.js")].decode().splitlines()
                if '"Font Families"' in line
            )
            self.assertIn(old_family_line, updated_text)
            self.assertEqual((game / "fonts" / SELECTED_FONT.name).read_bytes(), SELECTED_FONT.read_bytes())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["coverage"]["missing_characters"], "")
            self.assertEqual(
                len(
                    [r for r in report["confirmed_references"] if r["context"] == "plugin_font_loader_asset"]
                ),
                3,
            )
            self.assertFalse(
                any(
                    r["old_value"] == "Popup-Italic" and r["new_value"] != "Popup-Italic"
                    for r in report["confirmed_references"]
                )
            )
            self.assertFalse(
                any(
                    r["reason"] == "unresolved_javascript_font_value" and "," in r["value"]
                    for r in report["review"]
                )
            )
            self.assertEqual(self._plan(game).mutations, ())
            restored = subprocess.run(
                [
                    *tool,
                    "restore",
                    "--game",
                    str(game),
                    "--state",
                    str(state),
                    "--output",
                    str(root / "restore.json"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(
                {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}, before
            )

    def test_invalid_lists_preserve_the_parameters_and_consumer(self) -> None:
        for families in ("Popup-Italic", "Popup-Italic,,Fancy Script"):
            with self.subTest(families=families), tempfile.TemporaryDirectory() as temporary:
                game = self._game(Path(temporary), families=families)
                plan = self._plan(game)
                self.assertEqual(plan.mutations, ())
                self.assertIn("invalid_plugin_font_loader_lists", [r.reason for r in plan.reviews])

    def test_missing_loader_asset_cannot_be_replaced_by_a_same_name_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = self._game(Path(temporary))
            (game / "elsewhere").mkdir()
            (game / "fonts" / "Popup-Italic.ttf").rename(game / "elsewhere" / "Popup-Italic.ttf")
            plan = self._plan(game)
            plugins = parse_plugins(self._updated_plugins(game, plan), "plugins.js")
            self.assertEqual(plugins[1].parameters["Font"], "Popup-Italic")
            self.assertEqual(
                plugins[0].parameters["Font Filenames"],
                " Popup-Italic.ttf, NotoSansCJKsc-Regular.otf , NotoSansCJKsc-Regular.otf ",
            )
            self.assertIn("unresolved_plugin_font_loader_asset", [r.reason for r in plan.reviews])
            self.assertFalse(any(a.value == "Popup-Italic" for a in plan.aliases))

    def test_inactive_or_unknown_plugin_does_not_prove_list_registration(self) -> None:
        for loader, active in (("YEP_LoadCustomFonts", False), ("OtherLoader", True)):
            with self.subTest(loader=loader, active=active), tempfile.TemporaryDirectory() as temporary:
                game = self._game(Path(temporary), loader=loader, active=active)
                plan = self._plan(game)
                self.assertFalse(any(a.basis == "plugin_font_loader" for a in plan.aliases))
                plugins = parse_plugins(self._updated_plugins(game, plan), "plugins.js")
                self.assertEqual(
                    plugins[0].parameters["Font Filenames"],
                    " Popup-Italic.ttf, Decorative.TTF , Fancy Script.ttf ",
                )
                self.assertTrue(
                    any(
                        r.reason == "unresolved_javascript_font_value" and "," in r.value
                        for r in plan.reviews
                    )
                )

    def test_loader_uses_the_selected_html_entry_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._game(root)
            (root / "package.json").write_text('{"main":"start.html"}', encoding="utf-8")
            (root / "start.html").write_text("<html></html>", encoding="utf-8")
            (root / "fonts").mkdir()
            for name in ("Popup-Italic.ttf", "Decorative.TTF", "Fancy Script.ttf"):
                (root / "fonts" / name).write_bytes(b"entry-font")
            plan = build_font_plan(game_root=root, content_root=content, selected_font=SELECTED_FONT)
            self.assertEqual(
                {a.asset for a in plan.aliases if a.basis == "plugin_font_loader"},
                {"fonts/Popup-Italic.ttf", "fonts/Decorative.TTF", "fonts/Fancy Script.ttf"},
            )
            self.assertTrue(any(m.relative_path == "fonts/NotoSansCJKsc-Regular.otf" for m in plan.mutations))
            self.assertFalse(
                any(m.relative_path == "game/fonts/NotoSansCJKsc-Regular.otf" for m in plan.mutations)
            )

    def test_selected_filename_is_encoded_for_the_loader_url(self) -> None:
        for name in ("Font#Special%20.otf", "Font,Special.otf"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                game = self._game(root)
                selected = root / name
                shutil.copyfile(SELECTED_FONT, selected)
                plan = self._plan(game, selected_font=selected)
                plugins = parse_plugins(self._updated_plugins(game, plan), "plugins.js")
                filenames = plugins[0].parameters["Font Filenames"]
                self.assertIsInstance(filenames, str)
                assert isinstance(filenames, str)
                self.assertEqual(
                    [unquote(item.strip()) for item in filenames.split(",")], [selected.name] * 3
                )
                self.assertTrue(any(m.relative_path == f"fonts/{selected.name}" for m in plan.mutations))

    def test_static_registration_with_unknown_asset_preserves_its_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game = self._game(Path(temporary), active=False)
            (game / "js" / "plugins" / "Popup.js").write_text(
                'Graphics.loadFont("Popup-Italic", "fonts/Missing.ttf");\n'
                'bitmap.fontFace = "Popup-Italic";\n',
                encoding="utf-8",
            )
            plan = self._plan(game)
            self.assertEqual(plan.mutations, ())
            self.assertIn("unresolved_font_loader_asset", [r.reason for r in plan.reviews])

    def test_unavailable_entry_or_loader_preserves_the_declared_families(self) -> None:
        for missing, reason in (
            ("package.json", "unresolved_plugin_font_loader_entry"),
            ("js/plugins/YEP_LoadCustomFonts.js", "active_plugin_script_missing"),
        ):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                game = self._game(Path(temporary))
                (game / missing).unlink()
                plan = self._plan(game)
                self.assertEqual(plan.mutations, ())
                self.assertIn(reason, [r.reason for r in plan.reviews])


if __name__ == "__main__":
    unittest.main()
