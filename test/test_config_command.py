import os
from unittest.mock import patch

import yaml

from beets import config, ui
from beets.test.helper import BeetsTestCase


class ConfigCommandTest(BeetsTestCase):
    def setUp(self):
        super().setUp()
        for k in ("VISUAL", "EDITOR"):
            if k in os.environ:
                del os.environ[k]

        temp_dir = self.temp_dir.decode()

        self.config_path = os.path.join(temp_dir, "config.yaml")
        with open(self.config_path, "w") as file:
            file.write("library: lib\n")
            file.write("option: value\n")
            file.write("password: password_value")

        self.cli_config_path = os.path.join(temp_dir, "cli_config.yaml")
        with open(self.cli_config_path, "w") as file:
            file.write("option: cli overwrite")

        config.clear()
        config["password"].redact = True
        config._materialized = False

    def _run_with_yaml_output(self, *args):
        output = self.run_with_output(*args)
        return yaml.safe_load(output)

    def test_show_user_config(self):
        output = self._run_with_yaml_output("config", "-c")

        self.assertEqual(output["option"], "value")
        self.assertEqual(output["password"], "password_value")

    def test_show_user_config_with_defaults(self):
        output = self._run_with_yaml_output("config", "-dc")

        self.assertEqual(output["option"], "value")
        self.assertEqual(output["password"], "password_value")
        self.assertEqual(output["library"], "lib")
        assert not output["import"]["timid"]

    def test_show_user_config_with_cli(self):
        output = self._run_with_yaml_output(
            "--config", self.cli_config_path, "config"
        )

        self.assertEqual(output["library"], "lib")
        self.assertEqual(output["option"], "cli overwrite")

    def test_show_redacted_user_config(self):
        output = self._run_with_yaml_output("config")

        self.assertEqual(output["option"], "value")
        self.assertEqual(output["password"], "REDACTED")

    def test_show_redacted_user_config_with_defaults(self):
        output = self._run_with_yaml_output("config", "-d")

        self.assertEqual(output["option"], "value")
        self.assertEqual(output["password"], "REDACTED")
        assert not output["import"]["timid"]

    def test_config_paths(self):
        output = self.run_with_output("config", "-p")

        paths = output.split("\n")
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], self.config_path)

    def test_config_paths_with_cli(self):
        output = self.run_with_output(
            "--config", self.cli_config_path, "config", "-p"
        )
        paths = output.split("\n")
        self.assertEqual(len(paths), 3)
        self.assertEqual(paths[0], self.cli_config_path)

    def test_edit_config_with_visual_or_editor_env(self):
        os.environ["EDITOR"] = "myeditor"
        with patch("os.execlp") as execlp:
            self.run_command("config", "-e")
        execlp.assert_called_once_with("myeditor", "myeditor", self.config_path)

        os.environ["VISUAL"] = ""  # empty environment variables gets ignored
        with patch("os.execlp") as execlp:
            self.run_command("config", "-e")
        execlp.assert_called_once_with("myeditor", "myeditor", self.config_path)

        os.environ["VISUAL"] = "myvisual"
        with patch("os.execlp") as execlp:
            self.run_command("config", "-e")
        execlp.assert_called_once_with("myvisual", "myvisual", self.config_path)

    def test_edit_config_with_automatic_open(self):
        with patch("beets.util.open_anything") as open:
            open.return_value = "please_open"
            with patch("os.execlp") as execlp:
                self.run_command("config", "-e")
        execlp.assert_called_once_with(
            "please_open", "please_open", self.config_path
        )

    def test_config_editor_not_found(self):
        with self.assertRaises(ui.UserError) as user_error:
            with patch("os.execlp") as execlp:
                execlp.side_effect = OSError("here is problem")
                self.run_command("config", "-e")
        assert "Could not edit configuration" in str(user_error.exception)
        assert "here is problem" in str(user_error.exception)

    def test_edit_invalid_config_file(self):
        with open(self.config_path, "w") as file:
            file.write("invalid: [")
        config.clear()
        config._materialized = False

        os.environ["EDITOR"] = "myeditor"
        with patch("os.execlp") as execlp:
            self.run_command("config", "-e")
        execlp.assert_called_once_with("myeditor", "myeditor", self.config_path)
