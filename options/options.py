# options/options.py
"""
Interface layer between the VATSIM Online plugin and the Options dialog.

Keeps QSettings persistence and dialog lifecycle out of the main plugin
file — vatsim_online.py only needs to call OptionsManager.show_dialog().
"""
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QDialog

from .options_dialog import Ui_Options

_SETTINGS_PREFIX = "VatsimOnline/options"


class OptionsDialog(QDialog, Ui_Options):
    """The Qt dialog itself — UI mixin combined with QDialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)


class OptionsManager:
    """
    Handles opening the Options dialog and persisting its values.

    Usage from the main plugin class:
        self.options_manager = OptionsManager(self.iface.mainWindow())
        ...
        self.option_action.triggered.connect(self.options_manager.show_dialog)
    """

    def __init__(self, parent_window):
        self._parent_window = parent_window
        self._settings = QSettings()

    def show_dialog(self):
        """Open the Options dialog, pre-filled with saved values."""
        dialog = OptionsDialog(self._parent_window)
        self._restore_settings(dialog)

        if dialog.exec_():  # OK pressed
            self._save_settings(dialog)

    def _restore_settings(self, dialog: OptionsDialog) -> None:
        """Populate dialog controls from previously saved values."""
        # Example — replace with your actual widget names:
        # dialog.some_line_edit.setText(
        #     self._settings.value(f"{_SETTINGS_PREFIX}/some_value", "")
        # )
        pass

    def _save_settings(self, dialog: OptionsDialog) -> None:
        """Persist dialog control values after the user clicks OK."""
        # Example — replace with your actual widget names:
        # self._settings.setValue(
        #     f"{_SETTINGS_PREFIX}/some_value",
        #     dialog.some_line_edit.text(),
        # )
        pass

    def get(self, key: str, default=None, value_type=None):
        """Convenience getter for other parts of the plugin to read a saved option."""
        if value_type is not None:
            return self._settings.value(f"{_SETTINGS_PREFIX}/{key}", default, type=value_type)
        return self._settings.value(f"{_SETTINGS_PREFIX}/{key}", default)