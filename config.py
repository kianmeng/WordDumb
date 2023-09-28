#!/usr/bin/env python3

import json
import webbrowser
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from calibre.constants import isfrozen, ismacos
from calibre.gui2 import Dispatcher
from calibre.gui2.threaded_jobs import ThreadedJob
from calibre.utils.config import JSONConfig
from PyQt6.QtCore import QObject, QRegularExpression, Qt
from PyQt6.QtGui import QIcon, QRegularExpressionValidator
from PyQt6.QtSql import QSqlDatabase
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .custom_lemmas import CustomLemmasDialog
from .deps import download_word_wise_file, install_deps, which_python
from .dump_lemmas import dump_spacy_docs
from .error_dialogs import GITHUB_URL, job_failed
from .import_lemmas import apply_imported_lemmas_data, export_lemmas_job
from .utils import (
    donate,
    dump_prefs,
    get_plugin_path,
    kindle_db_path,
    load_languages_data,
    load_plugin_json,
    run_subprocess,
    spacy_model_name,
    wiktionary_db_path,
)

prefs = JSONConfig("plugins/worddumb")
prefs.defaults["use_pos"] = True
prefs.defaults["search_people"] = False
prefs.defaults["model_size"] = "md"
prefs.defaults["zh_wiki_variant"] = "cn"
prefs.defaults["fandom"] = ""
prefs.defaults["add_locator_map"] = False
prefs.defaults["preferred_formats"] = ["KFX", "AZW3", "AZW", "MOBI", "EPUB"]
prefs.defaults["use_all_formats"] = False
prefs.defaults["minimal_x_ray_count"] = 1
prefs.defaults["en_ipa"] = "ga_ipa"
prefs.defaults["zh_ipa"] = "pinyin"
prefs.defaults["choose_format_manually"] = True
prefs.defaults["wiktionary_gloss_lang"] = "en"
prefs.defaults["kindle_gloss_lang"] = "en"
prefs.defaults["use_gpu"] = False
prefs.defaults["cuda"] = "cu118"
prefs.defaults["last_opened_kindle_lemmas_language"] = "ca"
prefs.defaults["last_opened_wiktionary_lemmas_language"] = "ca"
prefs.defaults["use_wiktionary_for_kindle"] = False
for code in load_plugin_json(get_plugin_path(), "data/languages.json").keys():
    prefs.defaults[f"{code}_wiktionary_difficulty_limit"] = 5

load_translations()  # type: ignore
if TYPE_CHECKING:
    _: Any


class ConfigWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.plugin_path = get_plugin_path()

        vl = QVBoxLayout()
        self.setLayout(vl)

        format_order_button = QPushButton(_("Preferred format order"), self)
        format_order_button.clicked.connect(self.open_format_order_dialog)
        vl.addWidget(format_order_button)

        customize_ww_button = QPushButton(_("Customize Kindle Word Wise"))
        customize_ww_button.clicked.connect(
            partial(self.open_choose_lemma_lang_dialog, is_kindle=True)
        )
        vl.addWidget(customize_ww_button)

        custom_wiktionary_button = QPushButton(_("Customize EPUB Wiktionary"))
        custom_wiktionary_button.clicked.connect(
            partial(self.open_choose_lemma_lang_dialog, is_kindle=False)
        )
        vl.addWidget(custom_wiktionary_button)

        self.use_pos_box = QCheckBox(_("Use POS type to find Word Wise definition"))
        self.use_pos_box.setChecked(prefs["use_pos"])
        vl.addWidget(self.use_pos_box)

        self.search_people_box = QCheckBox(
            _("Fetch X-Ray people descriptions from Wikipedia/Fandom")
        )
        self.search_people_box.setToolTip(
            _(
                "Enable this option for nonfiction books and novels that have character"
                " pages on Wikipedia/Fandom"
            )
        )
        self.search_people_box.setChecked(prefs["search_people"])
        vl.addWidget(self.search_people_box)

        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        if not ismacos:
            self.use_gpu_box = QCheckBox(_("Run spaCy with GPU(requires CUDA)"))
            self.use_gpu_box.setToolTip(
                _(
                    "GPU will be used when creating X-Ray file if spaCy has transformer"
                    " model for the book language with ner component."
                )
            )
            self.use_gpu_box.setChecked(prefs["use_gpu"])
            vl.addWidget(self.use_gpu_box)

            cuda_versions = {"cu118": "CUDA 11.8", "cu117": "CUDA 11.7"}
            self.cuda_version_box = QComboBox()
            for cuda_version, text in cuda_versions.items():
                self.cuda_version_box.addItem(text, cuda_version)
            self.cuda_version_box.setCurrentText(cuda_versions[prefs["cuda"]])
            cuda_version_label = QLabel(_("CUDA version"))
            cuda_version_label.setToolTip(
                _('Use command "nvcc --version" to check CUDA version')
            )
            form_layout.addRow(cuda_version_label, self.cuda_version_box)

        model_size_label = QLabel(
            _('<a href="https://spacy.io/models/en">spaCy model</a> size')
        )
        model_size_label.setOpenExternalLinks(True)
        model_size_label.setToolTip(_("Larger model improves X-Ray quality"))
        self.model_size_box = QComboBox()
        spacy_model_sizes = {"sm": _("Small"), "md": _("Medium"), "lg": _("Large")}
        for size, text in spacy_model_sizes.items():
            self.model_size_box.addItem(text, size)
        self.model_size_box.setCurrentText(spacy_model_sizes[prefs["model_size"]])
        form_layout.addRow(model_size_label, self.model_size_box)

        self.minimal_x_ray_count = QSpinBox()
        self.minimal_x_ray_count.setMinimum(1)
        self.minimal_x_ray_count.setValue(prefs["minimal_x_ray_count"])
        minimal_x_ray_label = QLabel(_("Minimal X-Ray occurrences"))
        minimal_x_ray_label.setToolTip(
            _(
                "X-Ray entities that appear less then this number and don't have "
                "description from Wikipedia/Fandom will be removed"
            )
        )
        form_layout.addRow(minimal_x_ray_label, self.minimal_x_ray_count)

        self.zh_wiki_box = QComboBox()
        zh_variants = {
            "cn": "大陆简体",
            "hk": "香港繁體",
            "mo": "澳門繁體",
            "my": "大马简体",
            "sg": "新加坡简体",
            "tw": "臺灣正體",
        }
        for variant, text in zh_variants.items():
            self.zh_wiki_box.addItem(text, variant)
        self.zh_wiki_box.setCurrentText(zh_variants[prefs["zh_wiki_variant"]])
        form_layout.addRow(_("Chinese Wikipedia variant"), self.zh_wiki_box)

        self.fandom_url = QLineEdit()
        self.fandom_url.setText(prefs["fandom"])
        self.fandom_url.setPlaceholderText("https://*.fandom.com[/language]")
        fandom_re = QRegularExpression(r"https:\/\/[\w-]+\.fandom\.com(\/[\w-]+)?")
        fandom_validator = QRegularExpressionValidator(fandom_re)
        self.fandom_url.setValidator(fandom_validator)
        form_layout.addRow(_("Fandom URL"), self.fandom_url)

        vl.addLayout(form_layout)

        self.locator_map_box = QCheckBox(_("Add locator map to EPUB footnotes"))
        self.locator_map_box.setToolTip(
            _("Enable this option if your e-reader supports image in footnotes")
        )
        self.locator_map_box.setChecked(prefs["add_locator_map"])
        vl.addWidget(self.locator_map_box)

        donate_button = QPushButton(QIcon.ic("donate.png"), "Tree-fiddy?")
        donate_button.clicked.connect(donate)
        vl.addWidget(donate_button)

        doc_button = QPushButton(_("Document"))
        doc_button.clicked.connect(self.open_document)
        vl.addWidget(doc_button)

        github_button = QPushButton(_("Source code"))
        github_button.clicked.connect(self.open_github)
        vl.addWidget(github_button)

    def open_document(self) -> None:
        webbrowser.open("https://xxyzz.github.io/WordDumb")

    def open_github(self) -> None:
        webbrowser.open(GITHUB_URL)

    def save_settings(self) -> None:
        prefs["use_pos"] = self.use_pos_box.isChecked()
        prefs["search_people"] = self.search_people_box.isChecked()
        prefs["model_size"] = self.model_size_box.currentData()
        prefs["zh_wiki_variant"] = self.zh_wiki_box.currentData()
        prefs["fandom"] = self.fandom_url.text().removesuffix("/")
        prefs["add_locator_map"] = self.locator_map_box.isChecked()
        prefs["minimal_x_ray_count"] = self.minimal_x_ray_count.value()
        if not ismacos:
            prefs["use_gpu"] = self.use_gpu_box.isChecked()
            prefs["cuda"] = self.cuda_version_box.currentData()

    def open_format_order_dialog(self):
        format_order_dialog = FormatOrderDialog(self)
        if format_order_dialog.exec():
            format_order_dialog.save()

    def open_choose_lemma_lang_dialog(self, is_kindle: bool = True) -> None:
        choose_lang_dlg = ChooseLemmaLangDialog(self, is_kindle)
        if choose_lang_dlg.exec():
            lemma_lang = choose_lang_dlg.lemma_lang_box.currentData()
            gloss_lang = choose_lang_dlg.gloss_lang_box.currentData()
            prefs[
                "kindle_gloss_lang" if is_kindle else "wiktionary_gloss_lang"
            ] = gloss_lang
            prefs[
                "last_opened_kindle_lemmas_language"
                if is_kindle
                else "last_opened_wiktionary_lemmas_language"
            ] = lemma_lang
            if is_kindle and lemma_lang == "en" and gloss_lang in ["en", "zh", "zh_cn"]:
                prefs[
                    "use_wiktionary_for_kindle"
                ] = choose_lang_dlg.use_wiktionary_box.isChecked()

            db_path = (
                kindle_db_path(self.plugin_path, lemma_lang, prefs)
                if is_kindle
                else wiktionary_db_path(self.plugin_path, lemma_lang, gloss_lang)
            )
            if not db_path.exists():
                self.run_threaded_job(
                    download_word_wise_file,
                    (is_kindle, lemma_lang, prefs),
                    _("Downloading Word Wise file"),
                )
            else:
                custom_lemmas_dlg = CustomLemmasDialog(
                    self, is_kindle, lemma_lang, gloss_lang, db_path
                )
                if custom_lemmas_dlg.exec():
                    QSqlDatabase.removeDatabase(custom_lemmas_dlg.db_connection_name)
                    self.run_threaded_job(
                        dump_lemmas_job,
                        (is_kindle, db_path, lemma_lang),
                        _("Saving customized lemmas"),
                    )
                elif hasattr(custom_lemmas_dlg, "import_lemmas_path"):
                    QSqlDatabase.removeDatabase(custom_lemmas_dlg.db_connection_name)
                    self.run_threaded_job(
                        import_lemmas_job,
                        (
                            Path(custom_lemmas_dlg.import_lemmas_path),
                            db_path,
                            custom_lemmas_dlg.retain_enabled_lemmas,
                            is_kindle,
                            lemma_lang,
                        ),
                        _("Saving customized lemmas"),
                    )
                elif hasattr(custom_lemmas_dlg, "export_path"):
                    QSqlDatabase.removeDatabase(custom_lemmas_dlg.db_connection_name)
                    self.run_threaded_job(
                        export_lemmas_job,
                        (
                            db_path,
                            Path(custom_lemmas_dlg.export_path),
                            custom_lemmas_dlg.only_export_enabled,
                            custom_lemmas_dlg.export_difficulty_limit,
                            is_kindle,
                            lemma_lang,
                            gloss_lang,
                        ),
                        _("Exporting customized lemmas"),
                    )
                else:
                    QSqlDatabase.removeDatabase(custom_lemmas_dlg.db_connection_name)

    def run_threaded_job(self, func, args, job_title):
        gui = self.parent()
        while gui.parent() is not None:
            gui = gui.parent()
        job = ThreadedJob(
            "WordDumb's dumb job",
            job_title,
            func,
            args,
            {},
            Dispatcher(partial(job_failed, parent=gui)),
            killable=False,
        )
        gui.job_manager.run_threaded_job(job)


def import_lemmas_job(
    import_path: Path,
    db_path: Path,
    retain_lemmas: bool,
    is_kindle: bool,
    lemma_lang: str,
    abort: Any = None,
    log: Any = None,
    notifications: Any = None,
) -> None:
    apply_imported_lemmas_data(db_path, import_path, retain_lemmas, lemma_lang)
    dump_lemmas_job(is_kindle, db_path, lemma_lang)


def dump_lemmas_job(
    is_kindle: bool,
    db_path: Path,
    lemma_lang: str,
    abort: Any = None,
    log: Any = None,
    notifications: Any = None,
) -> None:
    plugin_path = get_plugin_path()
    model_name = spacy_model_name(
        lemma_lang, load_plugin_json(plugin_path, "data/languages.json"), prefs
    )
    install_deps(model_name, notifications)
    if isfrozen:
        options = {
            "is_kindle": is_kindle,
            "db_path": str(db_path),
            "lemma_lang": lemma_lang,
            "plugin_path": str(plugin_path),
            "model_name": model_name,
        }
        args = [
            which_python()[0],
            str(plugin_path),
            json.dumps(options),
            dump_prefs(prefs),
        ]
        run_subprocess(args)
    else:
        dump_spacy_docs(model_name, is_kindle, lemma_lang, db_path, plugin_path, prefs)


class FormatOrderDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Preferred format order"))
        vl = QVBoxLayout()
        self.setLayout(vl)

        self.format_list = QListWidget()
        self.format_list.setAlternatingRowColors(True)
        self.format_list.setDragEnabled(True)
        self.format_list.viewport().setAcceptDrops(True)
        self.format_list.setDropIndicatorShown(True)
        self.format_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.format_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.format_list.addItems(prefs["preferred_formats"])
        vl.addWidget(self.format_list)

        self.choose_format_maunally = QCheckBox(_("Choose format manually"))
        self.choose_format_maunally.setChecked(prefs["choose_format_manually"])
        self.choose_format_maunally.stateChanged.connect(
            self.disable_all_formats_button
        )
        vl.addWidget(self.choose_format_maunally)

        self.use_all_formats = QCheckBox(_("Create files for all available formats"))
        self.use_all_formats.setChecked(prefs["use_all_formats"])
        self.disable_all_formats_button(self.choose_format_maunally.checkState().value)
        vl.addWidget(self.use_all_formats)

        save_button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_button_box.accepted.connect(self.accept)
        save_button_box.rejected.connect(self.reject)
        vl.addWidget(save_button_box)

    def save(self):
        prefs["preferred_formats"] = [
            self.format_list.item(index).text()
            for index in range(self.format_list.count())
        ]
        prefs["choose_format_manually"] = self.choose_format_maunally.isChecked()
        prefs["use_all_formats"] = self.use_all_formats.isChecked()

    def disable_all_formats_button(self, choose_format_state: int) -> None:
        if choose_format_state == Qt.CheckState.Checked.value:
            self.use_all_formats.setChecked(False)
            self.use_all_formats.setDisabled(True)
        else:
            self.use_all_formats.setEnabled(True)


class ChooseFormatDialog(QDialog):
    def __init__(self, formats: list[str]) -> None:
        super().__init__()
        self.setWindowTitle(_("Choose book format"))
        vl = QVBoxLayout()
        self.setLayout(vl)

        message = QLabel(
            _(
                "This book has multiple supported formats. Choose the format "
                "you want to use."
            )
        )
        vl.addWidget(message)

        self.choose_format_manually = QCheckBox(
            _("Always ask when more than one format is available")
        )
        self.choose_format_manually.setChecked(True)
        vl.addWidget(self.choose_format_manually)

        format_buttons = QDialogButtonBox()
        for book_format in formats:
            button = format_buttons.addButton(
                book_format, QDialogButtonBox.ButtonRole.AcceptRole
            )
            button.clicked.connect(partial(self.accept_format, button.text()))
        vl.addWidget(format_buttons)

    def accept_format(self, chosen_format: str) -> None:
        self.chosen_format = chosen_format
        if not self.choose_format_manually.isChecked():
            prefs["choose_format_manually"] = False
        self.accept()


class ChooseLemmaLangDialog(QDialog):
    def __init__(self, parent: QObject, is_kindle: bool):
        super().__init__(parent)
        self.setWindowTitle(_("Choose language"))
        self.prefer_gloss_code = prefs[
            "kindle_gloss_lang" if is_kindle else "wiktionary_gloss_lang"
        ]

        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        self.lemma_lang_box = QComboBox()
        self.gloss_lang_box = QComboBox()
        language_dict = load_plugin_json(get_plugin_path(), "data/languages.json")
        for code in language_dict.keys():
            self.lemma_lang_box.addItem(_(language_dict[code]["name"]), code)
        self.lemma_lang_box.currentIndexChanged.connect(self.lemma_lang_changed)
        lemma_code = prefs[
            "last_opened_kindle_lemmas_language"
            if is_kindle
            else "last_opened_wiktionary_lemmas_language"
        ]
        self.lemma_lang_box.setCurrentText(_(language_dict[lemma_code]["name"]))
        self.lemma_lang_changed()
        form_layout.addRow(_("Lemma language"), self.lemma_lang_box)
        form_layout.addRow(_("Gloss language"), self.gloss_lang_box)

        if is_kindle:
            self.use_wiktionary_box = QCheckBox("")
            self.kindle_lang_changed()
            self.lemma_lang_box.currentIndexChanged.connect(self.kindle_lang_changed)
            self.gloss_lang_box.currentIndexChanged.connect(self.kindle_lang_changed)
            wiktionary_gloss_label = QLabel(_("Use Wiktionary definition"))
            wiktionary_gloss_label.setToolTip(
                _(
                    "Change Word Wise language to Chinese on your Kindle device to "
                    "view definition from Wiktionary"
                )
            )
            form_layout.addRow(wiktionary_gloss_label, self.use_wiktionary_box)

        confirm_button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        confirm_button_box.accepted.connect(self.accept)
        confirm_button_box.rejected.connect(self.reject)

        vl = QVBoxLayout()
        vl.addLayout(form_layout)
        vl.addWidget(confirm_button_box)
        self.setLayout(vl)

    def kindle_lang_changed(self) -> None:
        if (
            self.lemma_lang_box.currentData() == "en"
            and self.gloss_lang_box.currentData() in ["en", "zh", "zh_cn"]
        ):
            self.use_wiktionary_box.setEnabled(True)
            self.use_wiktionary_box.setChecked(prefs["use_wiktionary_for_kindle"])
        else:
            self.use_wiktionary_box.setChecked(True)
            self.use_wiktionary_box.setDisabled(True)

    def lemma_lang_changed(self) -> None:
        language_dict = load_languages_data(get_plugin_path())
        lemma_code = self.lemma_lang_box.currentData()
        self.gloss_lang_box.clear()
        available_gloss_codes = set()
        for code, value in language_dict.items():
            if "lemma_languages" in value and lemma_code in value["lemma_languages"]:
                self.gloss_lang_box.addItem(_(value["name"]), code)
                available_gloss_codes.add(code)
        if self.prefer_gloss_code in available_gloss_codes:
            self.gloss_lang_box.setCurrentText(
                _(language_dict[self.prefer_gloss_code]["name"])
            )
