# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import itertools
import os
import tempfile
from typing import TYPE_CHECKING

from django.core.management.utils import find_command
from django.utils.translation import gettext_lazy

from weblate.addons.base import BaseAddon, StoreBaseAddon, UpdateBaseAddon
from weblate.addons.events import AddonEvent
from weblate.addons.forms import (
    GenerateMoForm,
    GettextCustomizeForm,
    MsgmergeForm,
    XGettextForm,
)
from weblate.formats.base import UpdateError
from weblate.formats.exporters import MoExporter
from weblate.trans.util import cleanup_path
from weblate.utils.state import STATE_FUZZY, STATE_TRANSLATED

if TYPE_CHECKING:
    from weblate.auth.models import User

if TYPE_CHECKING:
    from weblate.trans.models import Component


class GettextBaseAddon(BaseAddon):
    compat = {"file_format": {"po", "po-mono"}}


class GenerateMoAddon(GettextBaseAddon):
    events = (AddonEvent.EVENT_PRE_COMMIT,)
    name = "weblate.gettext.mo"
    verbose = gettext_lazy("Generate MO files")
    description = gettext_lazy(
        "Automatically generates a MO file for every changed PO file."
    )
    settings_form = GenerateMoForm

    def pre_commit(self, translation, author) -> None:
        exporter = MoExporter(translation=translation)

        if self.instance.configuration.get("fuzzy"):
            state = STATE_FUZZY
        else:
            state = STATE_TRANSLATED
        units = translation.unit_set.filter(state__gte=state)

        exporter.add_units(units.prefetch_full())

        template = self.instance.configuration.get("path")
        if not template:
            template = "{{ filename|stripext }}.mo"

        output = self.render_repo_filename(template, translation)
        if not output:
            return

        with open(output, "wb") as handle:
            handle.write(exporter.serialize())
        translation.addon_commit_files.append(output)


class UpdateLinguasAddon(GettextBaseAddon):
    events = (AddonEvent.EVENT_POST_ADD, AddonEvent.EVENT_DAILY)
    name = "weblate.gettext.linguas"
    verbose = gettext_lazy("Update LINGUAS file")
    description = gettext_lazy(
        "Updates the LINGUAS file when a new translation is added."
    )

    @staticmethod
    def get_linguas_path(component):
        base = component.get_new_base_filename()
        if not base:
            base = os.path.join(
                component.full_path, component.filemask.replace("*", "x")
            )
        return os.path.join(os.path.dirname(base), "LINGUAS")

    @classmethod
    def can_install(cls, component, user):
        if not super().can_install(component, user):
            return False
        path = cls.get_linguas_path(component)
        return path and os.path.exists(path)

    @staticmethod
    def update_linguas(lines, codes):
        changed = False
        remove = []

        for i, line in enumerate(lines):
            # Split at comment and strip whitespace
            stripped = line.split("#", 1)[0].strip()
            # Comment/blank lines
            if not stripped:
                continue
            # Languages in one line
            if " " in stripped:
                expected = " ".join(sorted(codes))
                if stripped != expected:
                    lines[i] = expected + "\n"
                    changed = True
                codes = set()
                break
            # Language is already there
            if stripped in codes:
                codes.remove(stripped)
            else:
                remove.append(i)

        # Remove no longer present codes
        if remove:
            for i in reversed(remove):
                del lines[i]
            changed = True

        # Add missing codes
        if codes:
            lines.extend(f"{code}\n" for code in codes)
            changed = True

        return changed, lines

    def sync_linguas(self, component, path):
        with open(path) as handle:
            lines = handle.readlines()

        codes = set(
            component.translation_set.exclude(
                language=component.source_language
            ).values_list("language_code", flat=True)
        )

        changed, lines = self.update_linguas(lines, codes)

        if changed:
            with open(path, "w") as handle:
                handle.writelines(lines)

        return changed

    def post_add(self, translation) -> None:
        with translation.component.repository.lock:
            path = self.get_linguas_path(translation.component)
            if self.sync_linguas(translation.component, path):
                translation.addon_commit_files.append(path)

    def daily(self, component) -> None:
        with component.repository.lock:
            path = self.get_linguas_path(component)
            if self.sync_linguas(component, path):
                self.commit_and_push(component, [path])


class UpdateConfigureAddon(GettextBaseAddon):
    events = (AddonEvent.EVENT_POST_ADD, AddonEvent.EVENT_DAILY)
    name = "weblate.gettext.configure"
    verbose = gettext_lazy('Update ALL_LINGUAS variable in the "configure" file')
    description = gettext_lazy(
        'Updates the ALL_LINGUAS variable in "configure", '
        '"configure.in" or "configure.ac" files, when a new translation is added.'
    )

    @staticmethod
    def get_configure_paths(component):
        base = component.full_path
        for name in ("configure", "configure.in", "configure.ac"):
            path = os.path.join(base, name)
            if os.path.exists(path):
                yield path

    @classmethod
    def can_install(cls, component, user) -> bool:
        if not super().can_install(component, user):
            return False
        for name in cls.get_configure_paths(component):
            try:
                with open(name) as handle:
                    if 'ALL_LINGUAS="' in handle.read():
                        return True
            except UnicodeDecodeError:
                continue
        return False

    def sync_linguas(self, component, paths):
        added = False
        codes = " ".join(
            component.translation_set.exclude(language_id=component.source_language_id)
            .values_list("language_code", flat=True)
            .order_by("language_code")
        )
        expected = f'ALL_LINGUAS="{codes}"\n'
        for path in paths:
            with open(path) as handle:
                lines = handle.readlines()

            for i, line in enumerate(lines):
                stripped = line.strip()
                # Comment
                if stripped.startswith("#"):
                    continue
                if not stripped.startswith('ALL_LINGUAS="'):
                    continue
                if line != expected:
                    lines[i] = expected
                    added = True

            if added:
                with open(path, "w") as handle:
                    handle.writelines(lines)

        return added

    def post_add(self, translation) -> None:
        with translation.component.repository.lock:
            paths = list(self.get_configure_paths(translation.component))
            if self.sync_linguas(translation.component, paths):
                translation.addon_commit_files.extend(paths)

    def daily(self, component) -> None:
        with component.repository.lock:
            paths = list(self.get_configure_paths(component))
            if self.sync_linguas(component, paths):
                self.commit_and_push(component, paths)


class MsgmergeAddon(GettextBaseAddon, UpdateBaseAddon):
    name = "weblate.gettext.msgmerge"
    verbose = gettext_lazy("Update PO files to match POT (msgmerge)")
    description = gettext_lazy(
        'Updates all PO files (as configured by "File mask") to match the '
        'POT file (as configured by "Template for new translations") using msgmerge.'
    )
    alert = "MsgmergeAddonError"
    settings_form = MsgmergeForm

    @classmethod
    def can_install(cls, component, user):
        if find_command("msgmerge") is None:
            return False
        return super().can_install(component, user)

    def get_msgmerge_args(self, component):
        args = []
        if not self.instance.configuration.get("fuzzy", True):
            args.append("--no-fuzzy-matching")
        if self.instance.configuration.get("previous", True):
            args.append("--previous")
        if self.instance.configuration.get("no_location", False):
            args.append("--no-location")

        # Apply gettext customize add-on configuration
        if customize_addon := component.get_addon(GettextCustomizeAddon.name):
            args.extend(customize_addon.addon.get_msgmerge_args(component))
        return args

    def update_translations(self, component, previous_head) -> None:
        # Run always when there is an alerts, there is a chance that
        # the update clears it.
        repository = component.repository
        if previous_head and not component.alert_set.filter(name=self.alert).exists():
            changes = repository.list_changed_files(
                repository.ref_to_remote.format(previous_head)
            )
            if component.new_base not in changes:
                component.log_info(
                    "%s addon skipped, new base was not updated in %s..%s",
                    self.name,
                    previous_head,
                    repository.last_revision,
                )
                return
        template = component.get_new_base_filename()
        if not template or not os.path.exists(template):
            self.alerts.append(
                {
                    "addon": self.name,
                    "command": "msgmerge",
                    "output": template,
                    "error": "Template for new translations not found",
                }
            )
            self.trigger_alerts(component)
            component.log_info("%s addon skipped, new base was not found", self.name)
            return
        args = self.get_msgmerge_args(component)
        for translation in component.translation_set.iterator():
            filename = translation.get_filename()
            if (
                (translation.is_source and not translation.is_template)
                or not filename
                or not os.path.exists(filename)
            ):
                continue
            try:
                component.file_format_cls.update_bilingual(
                    filename, template, args=args
                )
            except UpdateError as error:
                self.alerts.append(
                    {
                        "addon": self.name,
                        "command": error.cmd,
                        "output": str(error.output),
                        "error": str(error),
                    }
                )
                component.log_info("%s addon failed: %s", self.name, error)
        self.trigger_alerts(component)

    def commit_and_push(
        self, component, files: list[str] | None = None, skip_push: bool = False
    ) -> None:
        if super().commit_and_push(component, files=files, skip_push=skip_push):
            component.create_translations()


class GettextCustomizeAddon(GettextBaseAddon, StoreBaseAddon):
    name = "weblate.gettext.customize"
    verbose = gettext_lazy("Customize gettext output")
    description = gettext_lazy(
        "Allows customization of gettext output behavior, for example line wrapping."
    )
    settings_form = GettextCustomizeForm

    def store_post_load(self, translation, store) -> None:
        store.store.wrapper.width = int(self.instance.configuration.get("width", 77))

    def get_msgmerge_args(self, component):
        if int(self.instance.configuration.get("width", 77)) != 77:
            return ["--no-wrap"]
        return []

    def get_xgettext_args(self, component):
        return self.get_msgmerge_args(component)


class GettextAuthorComments(GettextBaseAddon):
    events = (AddonEvent.EVENT_PRE_COMMIT,)
    name = "weblate.gettext.authors"
    verbose = gettext_lazy("Contributors in comment")
    description = gettext_lazy(
        "Updates the comment part of the PO file header to include contributor names "
        "and years of contributions."
    )

    def pre_commit(self, translation, author) -> None:
        if "noreply@weblate.org" in author:
            return
        if "<" in author:
            name, email = author.split("<")
            name = name.strip()
            email = email.rstrip(">")
        else:
            name = author
            email = None

        translation.store.store.updatecontributor(name, email)
        translation.store.save()


class XGettextAddon(GettextBaseAddon):
    name = "weblate.gettext.xgettext"
    verbose = gettext_lazy("Update POT file (xgettext)")
    description = gettext_lazy("Updates POT file using xgettext.")
    alert = "XGettextAddonError"
    settings_form = XGettextForm
    events = (AddonEvent.EVENT_POST_UPDATE,)

    @classmethod
    def can_install(cls, component: Component, user: User) -> bool:
        if not super().can_install(component, user):
            return False
        return component.new_base and find_command("xgettext")

    def get_directory(self) -> str:
        path = self.instance.configuration.get("directory", "")
        return "" if path == "." else path

    def get_files_from(self, component: Component) -> str | None:
        base = component.get_new_base_filename()
        if base is None:
            return None
        filename = self.instance.configuration.get("files_from", "POTFILES.in")
        return os.path.join(os.path.dirname(base), filename)

    def get_xgettext_args(self, component: Component) -> list[str]:
        args = [f"--files-from={self.get_files_from(component)}"]
        if directory := self.get_directory():
            args.append(f"--directory={directory}")
        if from_code := self.instance.configuration.get("from_code", "UTF-8"):
            args.append(f"--from-code={from_code}")
        if self.instance.configuration.get("add_comments", False):
            args.append("--add-comments")
        else:
            comment_tags = self.instance.configuration.get("add_comments_tags", "")
            args.extend(
                f"--add-comments={tag}" for tag in comment_tags.splitlines() if tag
            )
        if self.instance.configuration.get("no_default_keywords", False):
            args.append("--keyword")
        args.extend(
            f"--keyword={keyword}"
            for keyword in self.instance.configuration.get("keywords", "").splitlines()
            if keyword
        )
        args.extend(
            f"--flag={flag}"
            for flag in self.instance.configuration.get("flags", "").splitlines()
            if flag
        )
        if bug_address := component.report_source_bugs:
            args.append(f"--msgid-bugs-address={bug_address}")
        # Apply gettext customize add-on configuration
        if customize_addon := component.get_addon(GettextCustomizeAddon.name):
            args.extend(customize_addon.addon.get_xgettext_args(component))
        return args

    @staticmethod
    def load_file_list(path: str) -> list[str]:
        # parse POTFILES.in like gettext does (gettext-tools/src/file-list.c)
        # https://git.savannah.gnu.org/gitweb/?p=gettext.git;a=blob;f=gettext-tools/src/file-list.c
        with open(path) as f:
            return [
                line.rstrip("\n").rstrip(" \t\r")
                for line in f
                if line and not line.startswith("#")
            ]

    @staticmethod
    def is_pot_creation_date(line: str) -> bool:
        if line is None:
            return False
        return line.startswith('"POT-Creation-Date: ') and line.endswith('\\n"\n')

    @staticmethod
    def pot_files_same(file_a: str, file_b: str) -> bool:
        with open(file_a) as f_a, open(file_b) as f_b:
            for line_a, line_b in itertools.zip_longest(f_a, f_b):
                if line_a != line_b:
                    if not XGettextAddon.is_pot_creation_date(line_a):
                        return False
                    if not XGettextAddon.is_pot_creation_date(line_b):
                        return False
        return True

    def post_update(
        self, component: Component, previous_head: str, skip_push: bool, child: bool
    ) -> None:
        output = self.get_new_base_filename()
        files_from = self.get_files_from(component)
        if not files_from or not os.path.exists(files_from):
            self.alerts.append(
                {
                    "addon": self.name,
                    "command": "xgettext",
                    "output": output,
                    "error": "Input list (POTFILES.in) file not found",
                }
            )
            self.trigger_alerts(component)
            component.log_info("%s addon skipped, input file list not found", self.name)
            return
        input_files = self.load_file_list(files_from)
        directory = self.get_directory()
        repo_files = {
            cleanup_path(os.path.join(directory, input_path))
            for input_path in input_files
        }
        # Run always when there are alerts, there is a chance that
        # the update clears it.
        repository = component.repository
        if previous_head and not component.alert_set.filter(name=self.alert).exists():
            changes = set(
                repository.list_changed_files(
                    repository.ref_to_remote.format(previous_head)
                )
            )
            if not (changes & repo_files):
                component.log_info(
                    "%s addon skipped, input files not updated in %s..%s",
                    self.name,
                    previous_head,
                    repository.last_revision,
                )
                return
        try:
            for repo_path in repo_files:
                component.repository.resolve_symlinks(repo_path)
        except (OSError, ValueError) as ex:
            self.alerts.append(
                {
                    "addon": self.name,
                    "command": "xgettext",
                    "output": output,
                    "error": str(ex),
                }
            )
            self.trigger_alerts(component)
            component.log_info(
                "%s addon skipped, input file list is invalid: %s", self.name, ex
            )
            return
        args = self.get_xgettext_args(component)
        with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(output), delete_on_close=False
        ) as tmpfile:
            tmpfile.close()
            self.execute_process(component, ["xgettext", *args, "-o", tmpfile.name])
            if self.alerts:
                self.trigger_alerts(component)
                return
            if os.path.exists(output) and self.pot_files_same(tmpfile.name, output):
                component.log_info("%s addon: no updates", self.name)
                return
            os.replace(tmpfile.name, output)
        self.commit_and_push(component, [self.new_base], skip_push=skip_push)
