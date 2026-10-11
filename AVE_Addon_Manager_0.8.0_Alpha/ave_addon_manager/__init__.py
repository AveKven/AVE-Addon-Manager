bl_info = {
    "name": "AVE Addon Manager",
    "author": "AVE Blender",
    "version": (0, 8, 0),
    "blender": (5, 2, 0),
    "location": "3D View > Sidebar (N) > AVE Addons",
    "description": "Alpha: organize existing N-panels by workspace and mode (EN/RU)",
    "support": "TESTING",
    "category": "System",
}

import bpy
import addon_utils
import json
import os
import tempfile
from datetime import datetime, timezone
from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, IntProperty,
    StringProperty,
)

SELF_ID = __name__
PAGE_SIZE = 12
# Runtime-only patch records. Nothing is modified on disk in third-party add-ons.
PANEL_PATCHES = {}  # panel class -> original poll attribute (or missing sentinel)
CONFLICTS = {}  # module -> error; no writes to other add-on settings
MISSING = object()
# Runtime approval is never serialized. Old saved filters never apply on startup.
SESSION_ARMED = False
SYNC_BUSY = False

# Language is local to AVE. It does not change Blender's UI language.
RU_TEXT = {
    'Language': 'Язык интерфейса', 'English': 'Английский', 'Russian': 'Русский',
    'Open Viewport > N > AVE Addons': 'Откройте 3D Viewport > N > AVE Addons',
    'Favorites and profiles are saved with Blender preferences.': 'Избранное и профили сохраняются в настройках Blender.',
    'Save Blender Preferences': 'Сохранить настройки Blender',
    'ADDON MANAGER': 'МЕНЕДЖЕР АДДОНОВ',
    'Visibility rules': 'Правила отображения',
    'Filter by workspace': 'Фильтр по рабочим пространствам',
    'Filter by Object / Edit mode': 'Фильтр по режимам Object / Edit',
    'Refresh N panels': 'Обновить N-панели',
    'All visible by default': 'По умолчанию всё видно',
    'Apply rules for this session': 'Применить правила на эту сессию',
    'Session: ACTIVE': 'Сессия: ПРАВИЛА АКТИВНЫ',
    'Session: SAFE / inactive': 'Сессия: БЕЗОПАСНЫЙ РЕЖИМ',
    'Safety and Backup': 'Безопасность и резервные копии',
    'Original snapshot': 'Исходный снимок',
    'Restore panels': 'Восстановить панели',
    'Export JSON': 'Экспорт JSON', 'Import JSON': 'Импорт JSON',
    'Backup: AVE data only, not addon settings': 'Копия содержит данные AVE, не настройки других аддонов',
    'Favorites only': 'Только избранные',
    'Use original addon behavior': 'Оригинальное поведение аддона',
    'Show in mode:': 'Показывать в режиме:',
    'Visible in workspaces:': 'Показывать в рабочих пространствах:',
    'Enable workspace/mode filters above': 'Включите фильтры рабочих пространств/режимов выше',
    'Nothing found. Change filters.': 'Ничего не найдено. Измените фильтры.',
    'Previous': 'Назад', 'Next': 'Далее',
    'Only active N-sidebar add-ons are listed.': 'Показаны только активные аддоны с N-панелями.',
    'Save Blender preferences after changes.': 'После изменений сохраните настройки Blender.',
    'Force synchronize': 'Принудительная синхронизация',
    'Panel conflict: native mode kept': 'Конфликт панели: сохранён исходный режим',
    'Author / Support': 'Автор / Поддержка',
    'Support development on Boosty': 'Поддержать разработку на Boosty',
    'AVE Blender on Boosty': 'AVE Blender на Boosty',
    'Interface language': 'Язык интерфейса',
    'Blender language is not changed': 'Язык самого Blender не изменяется',
    'This is an ALPHA build. Please test on a backup configuration.': 'Это ALPHA-версия. Тестируйте на резервной конфигурации.',
    'Auto': 'Авто', 'Object': 'Объект', 'Edit': 'Правка',
    'Always': 'Всегда', 'Hidden': 'Скрыть',
}

def tr(text):
    try:
        settings = bpy.context.preferences.addons.get(SELF_ID)
        lang = settings.preferences.ui_language if settings else 'EN'
    except Exception:
        lang = 'EN'
    return RU_TEXT.get(text, text) if lang == 'RU' else text

MODE_ITEMS = [
    ('AUTO', 'Auto', 'Use the original panel behavior'),
    ('OBJECT', 'Object', 'Show N panels only in Object Mode'),
    ('EDIT', 'Edit', 'Show N panels only in Edit Mode'),
    ('ALWAYS', 'Always', 'Keep N panels available in every mode'),
    ('HIDE', 'Hidden', 'Hide the addon N panels (addon remains enabled)'),
]


MODE_ITEMS_RU = [
    ('AUTO', 'Авто', 'Использовать исходное поведение панели'),
    ('OBJECT', 'Объект', 'Показывать только в режиме объекта'),
    ('EDIT', 'Правка', 'Показывать только в режиме редактирования'),
    ('ALWAYS', 'Всегда', 'Не добавлять ограничений по режиму'),
    ('HIDE', 'Скрыть', 'Скрыть N-панели, не отключая аддон'),
]

def mode_items_by_language(self, context):
    return MODE_ITEMS_RU if (prefs() and prefs().ui_language == 'RU') else MODE_ITEMS


def addon_mode(pref, mid):
    item = pref.entries.get(mid)
    return item.n_mode if item else 'AUTO'


def is_edit_mode(context):
    return str(getattr(context, 'mode', '')).startswith('EDIT_')


def allowed_by_mode(rule, context):
    if rule in {'AUTO', 'ALWAYS'}:
        return True
    if rule == 'HIDE':
        return False
    if rule == 'OBJECT':
        return context.mode == 'OBJECT'
    if rule == 'EDIT':
        return is_edit_mode(context)
    return True


def panel_owner(cls, installed):
    # Only touch panel classes clearly owned by an ENABLED external addon.
    module = getattr(cls, '__module__', '')
    if not module or module in {'__main__', SELF_ID} or module.startswith('bl_ui.'):
        return None
    matches = [mid for mid in installed if module == mid or module.startswith(mid + '.')]
    return max(matches, key=len) if matches else None


def all_panel_classes():
    found, queue = set(), list(bpy.types.Panel.__subclasses__())
    while queue:
        cls = queue.pop()
        if cls in found:
            continue
        found.add(cls)
        queue.extend(cls.__subclasses__())
    return found


def original_poll_result(cls, context, original):
    if original is MISSING:
        # Class may inherit a poll method from one of its mixins.
        for base in cls.__mro__[1:]:
            if 'poll' in base.__dict__:
                return bool(getattr(base, 'poll')(context))
        return True
    # classmethod wrappers are bound through descriptor lookup.
    return bool(original.__get__(None, cls)(context))


def _registered_panel(cls):
    try:
        return bool(getattr(cls, 'is_registered', False))
    except Exception:
        return False


def _panel_order(classes):
    """Parents first (RNA inheritance and bl_parent_id)."""
    classes = set(classes)
    ordered = []
    pending = set(classes)
    for _ in range(len(classes) + 1):
        progressed = False
        for cls in tuple(pending):
            parents = [p for p in cls.__mro__[1:] if p in classes]
            pid = getattr(cls, 'bl_parent_id', '')
            parents.extend(p for p in classes if getattr(p, 'bl_idname', None) == pid and p is not cls)
            if all(p in ordered for p in parents):
                ordered.append(cls)
                pending.remove(cls)
                progressed = True
        if not pending or not progressed:
            break
    ordered.extend(sorted(pending, key=lambda c: c.__name__))
    return ordered


def _set_poll(cls, value):
    if value is MISSING:
        if 'poll' in cls.__dict__:
            delattr(cls, 'poll')
    else:
        setattr(cls, 'poll', value)


def _apply_panel_changes(changes):
    """Apply only real differences; transactional rollback of touched panels.

    RNA panel re-registration is inherently a compatibility risk. If anything
    fails, restore old descriptors, then attempt to restore registrations.
    """
    changes = {cls: desired for cls, desired in changes.items()
               if cls.__dict__.get('poll', MISSING) is not desired}
    if not changes:
        return []
    original_values = {cls: cls.__dict__.get('poll', MISSING) for cls in changes}
    registered = set(cls for cls in changes if _registered_panel(cls))
    order = _panel_order(registered)
    removed = []
    failures = []
    try:
        for cls in reversed(order):
            bpy.utils.unregister_class(cls)
            removed.append(cls)
        for cls, value in changes.items():
            _set_poll(cls, value)
        for cls in order:
            bpy.utils.register_class(cls)
    except Exception as ex:
        failures.append(('transaction', str(ex)))
        print('AVE: failed to change panels, rolling back:', ex)
        # Roll back all touched classes, even ones already re-registered.
        for cls in reversed(order):
            if _registered_panel(cls):
                try:
                    bpy.utils.unregister_class(cls)
                except Exception as e:
                    failures.append((cls.__name__, 'rollback unregister: ' + str(e)))
        for cls, value in original_values.items():
            try:
                _set_poll(cls, value)
            except Exception as e:
                failures.append((cls.__name__, 'rollback descriptor: ' + str(e)))
        for cls in order:
            if not _registered_panel(cls):
                try:
                    bpy.utils.register_class(cls)
                except Exception as e:
                    failures.append((cls.__name__, 'rollback registration: ' + str(e)))
        return failures
    return []


def workspace_visible(pref, mid, context):
    ws = getattr(getattr(context, 'workspace', None), 'name', '')
    if not ws:
        return True
    record = pref.entries.get(mid)
    if not record:
        return True
    try:
        hidden = json.loads(record.hidden_workspaces)
    except (TypeError, ValueError):
        return True
    return ws not in hidden


def _mode_poll_factory(original, owner):
    """Evaluate workspace and mode dynamically; no re-registration on a workspace switch."""
    def mode_poll(panel_cls, context):
        try:
            pref = prefs()
            if pref.use_workspace_visibility and not workspace_visible(pref, owner, context):
                return False
            if pref.use_mode_visibility and not allowed_by_mode(addon_mode(pref, owner), context):
                return False
        except (KeyError, AttributeError, RuntimeError):
            pass  # Fail open so users cannot lose panels due to manager errors.
        return original_poll_result(panel_cls, context, original)
    return classmethod(mode_poll)


def restore_mode_rules():
    global SESSION_ARMED
    SESSION_ARMED = False
    if not PANEL_PATCHES:
        return True
    previous = dict(PANEL_PATCHES)
    failures = _apply_panel_changes(previous)
    if failures:
        CONFLICTS['__restore__'] = str(failures[:3])
        print('AVE: some panels could not be restored; restart Blender:', failures)
        return False
    PANEL_PATCHES.clear()
    _redraw_n_panels()
    return True


def _redraw_n_panels():
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def sync_mode_rules():
    global SYNC_BUSY
    if SYNC_BUSY:
        return
    SYNC_BUSY = True
    try:
        _sync_mode_rules_impl()
    finally:
        SYNC_BUSY = False


def _sync_mode_rules_impl():
    try:
        pref = prefs()
    except (KeyError, AttributeError):
        return
    # On Blender startup or after importing a configuration, AVE is passive.
    if not SESSION_ARMED:
        return
    installed = set(bpy.context.preferences.addons.keys())
    target = {}
    originals = {}
    if pref.use_mode_visibility or pref.use_workspace_visibility:
        for cls in all_panel_classes():
            try:
                if not (_registered_panel(cls)
                        and getattr(cls, 'bl_space_type', '') == 'VIEW_3D'
                        and getattr(cls, 'bl_region_type', '') == 'UI'):
                    continue
                mid = panel_owner(cls, installed)
                if not mid or mid == SELF_ID or mid in CONFLICTS:
                    continue
                record = pref.entries.get(mid)
                if not record or record.native_behavior:
                    continue
                rule = addon_mode(pref, mid)
                has_ws = record.hidden_workspaces not in ('', '[]')
                if not ((pref.use_mode_visibility and rule not in {'AUTO', 'ALWAYS'})
                        or (pref.use_workspace_visibility and has_ws)):
                    continue
                original = PANEL_PATCHES.get(cls, cls.__dict__.get('poll', MISSING))
                originals[cls] = original
                if cls not in PANEL_PATCHES:
                    target[cls] = _mode_poll_factory(original, mid)
            except (TypeError, AttributeError, RuntimeError) as e:
                print('AVE: skipping unsupported panel:', e)
    # NEVER re-register a panel already correctly patched. The poll closure
    # reads current rules on redraw, so workspace/mode switches need no RNA edits.
    changes = {cls: original for cls, original in PANEL_PATCHES.items() if cls not in originals}
    changes.update(target)
    if not changes:
        _redraw_n_panels()
        return
    try:
        _ensure_backup()
    except Exception as e:
        CONFLICTS['__backup__'] = str(e)
        print('AVE: refusing to change panels without a backup:', e)
        return
    failures = _apply_panel_changes(changes)
    if failures:
        # Transaction rolled back; current patch table remains valid.
        for cls in changes:
            mid = panel_owner(cls, installed)
            if mid:
                CONFLICTS[mid] = str(failures[0])
        _redraw_n_panels()
        return
    for cls in list(PANEL_PATCHES):
        if cls not in originals:
            PANEL_PATCHES.pop(cls, None)
    for cls in target:
        PANEL_PATCHES[cls] = originals[cls]
    _redraw_n_panels()


def _deferred_sync():
    try:
        sync_mode_rules()
    except Exception as ex:
        print('AVE Addon Manager: sync failed:', ex)
    return None


def update_mode_visibility(self, context):
    # Avoid unregistering Blender UI classes within an active draw/update callback.
    if not bpy.app.timers.is_registered(_deferred_sync):
        bpy.app.timers.register(_deferred_sync, first_interval=0.2)



def prefs():
    return bpy.context.preferences.addons[SELF_ID].preferences


def addon_entries():
    """Metadata only; don't import disabled add-ons."""
    try:
        mods = addon_utils.modules(refresh=False)
    except Exception:
        mods = addon_utils.modules()
    records = {}
    for mod in mods:
        mid = mod.__name__
        info = addon_utils.module_bl_info(mod)
        records[mid] = {
            "name": str(info.get("name") or mid),
            "author": str(info.get("author") or ""),
            "category": str(info.get("category") or ""),
            "description": str(info.get("description") or ""),
        }
    # Always include already enabled add-ons, even if metadata scan omits one.
    for mid in bpy.context.preferences.addons.keys():
        records.setdefault(mid, {"name": mid.rsplit(".", 1)[-1], "author": "", "category": "", "description": ""})
    return records


def native_tabs():
    """Discover EXISTING sidebar categories from registered Blender panels.

    Does not create or rename tabs. Keys are real Panel.bl_category strings.
    """
    installed = set(bpy.context.preferences.addons.keys())
    tabs = {}
    addon_tabs = {}
    for cls in all_panel_classes():
        try:
            if not _registered_panel(cls):
                continue
            if getattr(cls, 'bl_space_type', '') != 'VIEW_3D' or getattr(cls, 'bl_region_type', '') != 'UI':
                continue
            category = str(getattr(cls, 'bl_category', '') or 'Item')
            if getattr(cls, '__module__', '') == SELF_ID:
                continue
            owner = panel_owner(cls, installed)
            tabs.setdefault(category, set())
            if owner and owner != SELF_ID:
                tabs[category].add(owner)
                addon_tabs.setdefault(owner, set()).add(category)
        except (AttributeError, TypeError, RuntimeError):
            continue
    return tabs, addon_tabs


def enabled(mid):
    return mid in bpy.context.preferences.addons


def entry(pref, mid):
    item = pref.entries.get(mid)
    if item is None:
        item = pref.entries.add()
        item.name = mid
    return item


def listed(pref, by_addon=None):
    result = []
    search = pref.search.casefold().strip()
    if by_addon is None:
        _, by_addon = native_tabs()
    for mid, data in addon_entries().items():
        # Only enabled add-ons that actually register a 3D View N-sidebar panel.
        if mid not in by_addon or not enabled(mid):
            continue
        if pref.filter_state == 'ON' and not enabled(mid):
            continue
        if pref.filter_state == 'OFF' and enabled(mid):
            continue
        meta = pref.entries.get(mid)
        if pref.favorites_only and not (meta and meta.favorite):
            continue
        if search and not any(search in str(v).casefold() for v in (mid, *data.values(), *by_addon.get(mid, set()))):
            continue
        result.append((mid, data, bool(meta and meta.favorite)))
    result.sort(key=lambda it: (not it[2], it[1]['name'].casefold(), it[0]))
    return result


class AVE_AddonEntry(bpy.types.PropertyGroup):
    favorite: BoolProperty(name="Favorite", default=False)
    expanded: BoolProperty(name="Details", default=False)
    n_mode: EnumProperty(name="N Panel", items=mode_items_by_language, default='AUTO', update=update_mode_visibility)
    hidden_workspaces: StringProperty(name="Hidden Workspaces", default="[]")
    native_behavior: BoolProperty(name="Use original behavior", default=True, update=update_mode_visibility)


class AVE_Profile(bpy.types.PropertyGroup):
    modules_json: StringProperty(default="[]")


class AVE_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = SELF_ID

    entries: CollectionProperty(type=AVE_AddonEntry)
    profiles: CollectionProperty(type=AVE_Profile)
    search: StringProperty(name="Search", description="Search name, identifier, author, category, or description")
    selected_workspace: StringProperty(name="Selected Workspace", default="")
    show_workspace: BoolProperty(name="Workspace visibility", default=True)
    use_workspace_visibility: BoolProperty(name="Control N-panels per workspace", default=False, update=update_mode_visibility)
    new_profile: StringProperty(name="New profile")
    active_profile: StringProperty(default="")
    filter_state: EnumProperty(name="Status", items=[('ALL', 'All', ''), ('ON', 'Enabled', ''), ('OFF', 'Disabled', '')], default='ALL')
    favorites_only: BoolProperty(name="Favorites only", default=False)
    page: IntProperty(default=0, min=0)
    show_profiles: BoolProperty(name="Profiles", default=True)
    use_mode_visibility: BoolProperty(name="Auto-hide N panel tabs by mode", default=False, update=update_mode_visibility)
    show_modes: BoolProperty(name="N panel modes", default=True)
    ui_language: EnumProperty(name='Interface language', items=[('EN', 'English', 'English UI'), ('RU', 'Русский', 'Русский интерфейс')], default='EN')
    show_author: BoolProperty(name='Author / Support', default=False)
    initial_backup_decided: BoolProperty(name="Initial backup prompt answered", default=False)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'ui_language', text=tr('Language'))
        layout.label(text=tr('Blender language is not changed'), icon='INFO')
        layout.separator()
        layout.label(text=tr('Open Viewport > N > AVE Addons'), icon='INFO')
        layout.label(text=tr('Favorites and profiles are saved with Blender preferences.'))
        layout.label(text=tr('This is an ALPHA build. Please test on a backup configuration.'), icon='ERROR')
        layout.operator('ave_addons.save_settings', text=tr('Save Blender Preferences'), icon='FILE_TICK')
        layout.separator()
        row = layout.row(align=True)
        row.alignment = 'RIGHT'
        row.operator('ave_addons.open_boosty', text=tr('Support development on Boosty'), icon='URL')


class AVE_OT_toggle(bpy.types.Operator):
    bl_idname = 'ave_addons.toggle'
    bl_label = 'Manage Add-on in Blender Preferences'
    module: StringProperty()
    def execute(self, context):
        self.report({'WARNING'}, 'AVE does not enable or disable other add-ons. Use Blender Preferences.')
        return {'CANCELLED'}


class AVE_OT_expand(bpy.types.Operator):
    bl_idname = "ave_addons.expand"
    bl_label = "Expand Add-on Settings"
    bl_description = "Show mode and workspace visibility settings"
    module: StringProperty()

    def execute(self, context):
        rec = entry(prefs(), self.module)
        rec.expanded = not rec.expanded
        return {'FINISHED'}


class AVE_OT_favorite(bpy.types.Operator):
    bl_idname = "ave_addons.favorite"
    bl_label = "Toggle Favorite"
    module: StringProperty()

    def execute(self, context):
        item = entry(prefs(), self.module)
        item.favorite = not item.favorite
        return {'FINISHED'}


class AVE_OT_workspace_select(bpy.types.Operator):
    bl_idname = "ave_addons.workspace_select"
    bl_label = "Select Workspace"
    workspace: StringProperty()

    def execute(self, context):
        if self.workspace in bpy.data.workspaces:
            prefs().selected_workspace = self.workspace
            prefs().page = 0
        return {'FINISHED'}


class AVE_OT_workspace_toggle(bpy.types.Operator):
    bl_idname = "ave_addons.workspace_toggle"
    bl_label = "Show / Hide Add-on in Workspace"
    bl_description = "Changes visibility of existing N-panels, not add-on enable state"
    module: StringProperty()
    workspace: StringProperty()

    def execute(self, context):
        if self.workspace not in bpy.data.workspaces:
            self.report({'ERROR'}, "Workspace no longer exists")
            return {'CANCELLED'}
        record = entry(prefs(), self.module)
        try:
            hidden = set(json.loads(record.hidden_workspaces))
        except (ValueError, TypeError):
            hidden = set()
        if self.workspace in hidden:
            hidden.remove(self.workspace)
        else:
            hidden.add(self.workspace)
        record.hidden_workspaces = json.dumps(sorted(hidden), ensure_ascii=False)
        update_mode_visibility(None, context)
        _redraw_n_panels()
        return {'FINISHED'}


class AVE_OT_workspace_show_all(bpy.types.Operator):
    bl_idname = "ave_addons.workspace_show_all"
    bl_label = "Show All N-panels"
    bl_description = "Clear workspace visibility rules for selected workspace"

    def execute(self, context):
        pref = prefs()
        workspace = pref.selected_workspace or context.workspace.name
        for record in pref.entries:
            try:
                hidden = set(json.loads(record.hidden_workspaces))
            except (TypeError, ValueError):
                hidden = set()
            if workspace in hidden:
                hidden.remove(workspace)
                record.hidden_workspaces = json.dumps(sorted(hidden), ensure_ascii=False)
        update_mode_visibility(None, context)
        return {'FINISHED'}


class AVE_OT_profile_save(bpy.types.Operator):
    bl_idname = "ave_addons.profile_save"
    bl_label = "Save Current Profile"
    bl_description = "Save a list of currently enabled add-ons"

    def execute(self, context):
        pref = prefs()
        name = pref.new_profile.strip()
        if not name:
            self.report({'WARNING'}, "Enter a profile name")
            return {'CANCELLED'}
        obj = pref.profiles.get(name)
        if obj is None:
            obj = pref.profiles.add()
            obj.name = name
        obj.modules_json = json.dumps(sorted(set(context.preferences.addons.keys())))
        pref.active_profile = name
        pref.new_profile = ""
        self.report({'INFO'}, f"Profile '{name}' saved")
        return {'FINISHED'}


class AVE_OT_profile_select(bpy.types.Operator):
    bl_idname = "ave_addons.profile_select"
    bl_label = "Select Profile"
    name: StringProperty()

    def execute(self, context):
        prefs().active_profile = self.name
        return {'FINISHED'}


class AVE_OT_profile_apply(bpy.types.Operator):
    bl_idname = "ave_addons.profile_apply"
    bl_label = "Apply Profile"
    bl_description = "Restore the saved enabled/disabled add-on states; may interrupt active tools"
    bl_options = {'REGISTER', 'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        self.report({'WARNING'}, 'Disabled for safety: AVE does not alter enabled add-on states')
        return {'CANCELLED'}


class AVE_OT_profile_remove(bpy.types.Operator):
    bl_idname = "ave_addons.profile_remove"
    bl_label = "Delete Profile"

    def execute(self, context):
        pref = prefs()
        idx = pref.profiles.find(pref.active_profile)
        if idx >= 0:
            pref.profiles.remove(idx)
        pref.active_profile = ""
        return {'FINISHED'}


class AVE_OT_page(bpy.types.Operator):
    bl_idname = "ave_addons.page"
    bl_label = "Change Page"
    delta: IntProperty(default=1)

    def execute(self, context):
        pref = prefs()
        count = len(listed(pref))
        max_page = max(0, (count - 1) // PAGE_SIZE)
        pref.page = min(max(0, pref.page + self.delta), max_page)
        return {'FINISHED'}


class AVE_OT_mode_init(bpy.types.Operator):
    bl_idname = 'ave_addons.mode_init'
    bl_label = 'Assign N-panel mode'
    module: StringProperty()

    def execute(self, context):
        entry(prefs(), self.module)
        return {'FINISHED'}


class AVE_OT_resync_modes(bpy.types.Operator):
    bl_idname = 'ave_addons.resync_modes'
    bl_label = 'Refresh N Panels'
    bl_description = 'Find newly enabled N-panels and apply mode visibility rules'

    def execute(self, context):
        update_mode_visibility(None, context)
        self.report({'INFO'}, 'N-panel rules refreshed')
        return {'FINISHED'}


class AVE_OT_refresh(bpy.types.Operator):
    bl_idname = "ave_addons.refresh"
    bl_label = "Refresh Installed Add-ons"

    def execute(self, context):
        try:
            addon_utils.modules(refresh=True)
        except Exception as ex:
            self.report({'WARNING'}, str(ex))
        return {'FINISHED'}


class AVE_OT_save_settings(bpy.types.Operator):
    bl_idname = "ave_addons.save_settings"
    bl_label = "Save Blender Preferences"
    bl_description = "Save favorites, profiles and current add-on states"

    def execute(self, context):
        try:
            bpy.ops.wm.save_userpref()
        except Exception as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        self.report({'INFO'}, "Blender preferences saved")
        return {'FINISHED'}



# Backups contain AVE configuration + descriptions of original panels. The
# original executable poll methods cannot be serialized/restored from JSON.
def _snapshot():
    pref = prefs()
    addons = {}
    for item in pref.entries:
        addons[item.name] = dict(favorite=bool(item.favorite), n_mode=item.n_mode,
                                 native_behavior=bool(item.native_behavior),
                                 hidden_workspaces=json.loads(item.hidden_workspaces or '[]'))
    _, tab_map = native_tabs()
    return dict(format='AVE_ADDON_MANAGER', schema=1, version='0.8.0-alpha',
                created=datetime.now(timezone.utc).isoformat(),
                addons=addons,
                workspaces=[w.name for w in bpy.data.workspaces],
                native_panels={k: sorted(v) for k,v in tab_map.items()},
                profiles={x.name: json.loads(x.modules_json or '[]') for x in pref.profiles},
                note='Only AVE visibility/favorite rules and panel metadata. No third-party preferences or executable poll functions.')


def _write_json(filepath, obj):
    filepath = os.path.abspath(bpy.path.abspath(filepath))
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.ave_', suffix='.tmp', dir=os.path.dirname(filepath))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(obj, file, indent=2, ensure_ascii=False)
        os.replace(tmp, filepath)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _backup_path():
    root = bpy.utils.user_resource('CONFIG', path='ave_addon_manager', create=True)
    return os.path.join(root, 'AVE_Addons_original.json')


def _ensure_backup():
    path = _backup_path()
    if not os.path.exists(path):
        _write_json(path, _snapshot())
    return path


class AVE_OT_backup(bpy.types.Operator):
    bl_idname = 'ave_addons.backup'
    bl_label = 'Save Original Snapshot'
    bl_description = 'Write a one-time backup of AVE rules and native panel metadata (not third-party add-on preferences)'
    def execute(self, context):
        try:
            path = _ensure_backup()
            self.report({'INFO'}, 'AVE snapshot: ' + path)
            return {'FINISHED'}
        except Exception as ex:
            self.report({'ERROR'}, 'Backup failed: ' + str(ex))
            return {'CANCELLED'}


class AVE_OT_open_boosty(bpy.types.Operator):
    bl_idname = 'ave_addons.open_boosty'
    bl_label = 'AVE Blender on Boosty'
    bl_description = 'Open https://boosty.to/ave_blender'
    def execute(self, context):
        bpy.ops.wm.url_open(url='https://boosty.to/ave_blender')
        return {'FINISHED'}


class AVE_OT_first_backup(bpy.types.Operator):
    bl_idname = 'ave_addons.first_backup'
    bl_label = 'Create Initial AVE Backup?'
    bl_description = 'Offer an initial snapshot of AVE rules and detected N-panel metadata'
    bl_options = {'INTERNAL'}
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=470)
    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text='Save original N-panel metadata and AVE rules?', icon='INFO')
        col.label(text='File: AVE_Addons_original.json')
        col.label(text='This is NOT a backup of other addons settings.')
        col.label(text='No files or preferences of other addons are modified.')
    def execute(self, context):
        try:
            path = _ensure_backup()
        except Exception as ex:
            self.report({'ERROR'}, 'Cannot create initial backup: ' + str(ex))
            return {'CANCELLED'}
        prefs().initial_backup_decided = True
        self.report({'INFO'}, 'Initial backup created: ' + path)
        return {'FINISHED'}
    def cancel(self, context):
        prefs().initial_backup_decided = True


class AVE_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'ave_addons.export'
    bl_label = 'Export AVE Settings'
    filename_ext = '.json'
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})
    filepath: StringProperty(subtype='FILE_PATH', default='AVE_Addons.json')
    def invoke(self, context, event):
        self.filepath = 'AVE_Addons.json'
        return ExportHelper.invoke(self, context, event)
    def execute(self, context):
        try:
            _write_json(self.filepath, _snapshot())
            self.report({'INFO'}, 'AVE settings exported')
            return {'FINISHED'}
        except Exception as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}


class AVE_OT_import(bpy.types.Operator, ImportHelper):
    bl_idname = 'ave_addons.import'
    bl_label = 'Import AVE Settings'
    bl_description = 'Import only AVE rules; unmatched add-ons retained for later use; never install or alter other add-ons'
    filename_ext = '.json'
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})
    def execute(self, context):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as file:
                data = json.load(file)
            if not isinstance(data, dict) or data.get('format') != 'AVE_ADDON_MANAGER' or data.get('schema') != 1:
                raise ValueError('This is not a supported AVE settings file')
            addons = data.get('addons', {})
            if not isinstance(addons, dict) or len(addons) > 10000:
                raise ValueError('Invalid addon entries')
            _ensure_backup()
            pref = prefs()
            for mid, rule in addons.items():
                if not isinstance(mid, str) or len(mid) > 250 or not isinstance(rule, dict):
                    continue
                item = entry(pref, mid)
                item.native_behavior = True  # remain safe until the user opts in
                item.favorite = bool(rule.get('favorite', False))
                mode = rule.get('n_mode', 'AUTO')
                item.n_mode = mode if mode in {'AUTO','OBJECT','EDIT','ALWAYS','HIDE'} else 'AUTO'
                workspaces = rule.get('hidden_workspaces', [])
                if isinstance(workspaces, list):
                    item.hidden_workspaces = json.dumps([x for x in workspaces[:1000] if isinstance(x,str) and len(x)<250], ensure_ascii=False)
                # imported rules are intentionally INACTIVE until opt-in
            update_mode_visibility(None, context)
            missing = len(set(addons) - set(addon_entries()))
            self.report({'INFO'}, f'Imported {len(addons)} AVE entries ({missing} not installed); native behavior stays ON')
            return {'FINISHED'}
        except Exception as ex:
            self.report({'ERROR'}, 'Import failed: ' + str(ex))
            return {'CANCELLED'}


class AVE_OT_force_sync(bpy.types.Operator):
    bl_idname = 'ave_addons.force_sync'
    bl_label = 'Force Synchronize'
    bl_description = 'Retry a conflicted add-on panel at your own risk; never changes its saved preferences'
    module: StringProperty()
    bl_options = {'REGISTER', 'INTERNAL'}
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    def execute(self, context):
        try:
            _ensure_backup()
            CONFLICTS.pop(self.module, None)
            rec = entry(prefs(), self.module)
            rec.native_behavior = False
            global SESSION_ARMED
            SESSION_ARMED = True
            sync_mode_rules()
            if self.module in CONFLICTS:
                self.report({'WARNING'}, 'Still incompatible: ' + CONFLICTS[self.module][:160])
            else:
                self.report({'INFO'}, 'Synchronization attempted')
            return {'FINISHED'}
        except Exception as ex:
            CONFLICTS[self.module] = str(ex)
            self.report({'WARNING'}, str(ex))
            return {'CANCELLED'}


class AVE_OT_activate_session(bpy.types.Operator):
    bl_idname = 'ave_addons.activate_session'
    bl_label = 'Apply AVE Visibility Rules'
    bl_description = 'Apply custom visibility for this Blender session only. Never auto-applies on startup.'
    bl_options = {'REGISTER', 'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        global SESSION_ARMED
        try:
            _ensure_backup()
        except Exception as ex:
            self.report({'ERROR'}, f'Backup failed; no changes made: {ex}')
            return {'CANCELLED'}
        SESSION_ARMED = True
        sync_mode_rules()
        if CONFLICTS:
            self.report({'WARNING'}, 'Some panels incompatible. See Safety warnings.')
        else:
            self.report({'INFO'}, 'Rules active for this session; restart restores native behavior')
        return {'FINISHED'}


class AVE_OT_restore_all(bpy.types.Operator):
    bl_idname = 'ave_addons.restore_all'
    bl_label = 'Restore Original Panel Behavior'
    bl_description = 'Disable AVE filters and restore runtime panel methods; third-party addon settings untouched'
    def execute(self, context):
        pref = prefs()
        pref.use_workspace_visibility = False
        pref.use_mode_visibility = False
        for record in pref.entries:
            record.native_behavior = True
        ok = restore_mode_rules()
        if ok:
            CONFLICTS.clear()
            self.report({'INFO'}, 'Original panel behavior restored')
        else:
            self.report({'WARNING'}, 'Could not restore every panel; restart Blender')
        return {'FINISHED'}


class AVE_PT_addons(bpy.types.Panel):
    bl_label = "AVE Addon Manager"
    bl_idname = "AVE_PT_addons"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AVE Addons"

    def draw(self, context):
        layout = self.layout
        pref = prefs()
        head = layout.row(align=True)
        head.label(text="ADDON MANAGER", icon='PREFERENCES')
        head.operator('ave_addons.refresh', text="", icon='FILE_REFRESH')
        head.operator('ave_addons.save_settings', text="", icon='FILE_TICK')

        tabs, addon_tabs = native_tabs()
        box = layout.box()
        box.label(text=tr('Visibility rules'), icon='RESTRICT_VIEW_OFF')
        box.prop(pref, 'use_workspace_visibility', text=tr('Filter by workspace'), toggle=True)
        box.prop(pref, 'use_mode_visibility', text=tr('Filter by Object / Edit mode'), toggle=True)
        box.operator('ave_addons.resync_modes', text=tr('Refresh N panels'), icon='FILE_REFRESH')
        box.label(text=tr('All visible by default'), icon='INFO')
        box.operator('ave_addons.activate_session', text=tr('Apply rules for this session'), icon='PLAY')
        box.label(text=tr('Session: ACTIVE') if SESSION_ARMED else tr('Session: SAFE / inactive'), icon='INFO')
        safety = layout.box()
        safety.label(text=tr('Safety and Backup'), icon='LOCKED')
        r = safety.row(align=True)
        r.operator('ave_addons.backup', text=tr('Original snapshot'), icon='FILE_TICK')
        r.operator('ave_addons.restore_all', text=tr('Restore panels'), icon='FILE_REFRESH')
        r = safety.row(align=True)
        r.operator('ave_addons.export', text=tr('Export JSON'), icon='EXPORT')
        r.operator('ave_addons.import', text=tr('Import JSON'), icon='IMPORT')
        safety.label(text=tr('Backup: AVE data only, not addon settings'), icon='INFO')
        if CONFLICTS:
            safety.label(text=(f'Конфликтов: {len(CONFLICTS)} — см. консоль' if pref.ui_language == 'RU' else f'{len(CONFLICTS)} panel conflict(s) - see console'), icon='ERROR')


        # Enabling/disabling add-ons via profiles is intentionally not exposed:
        # AVE must never alter third-party enable states.
        layout.separator()
        layout.prop(pref, 'search', text='', icon='VIEWZOOM')
        layout.prop(pref, 'favorites_only', text=tr('Favorites only'), icon='SOLO_ON')
        selected_ws = pref.selected_workspace if pref.selected_workspace in bpy.data.workspaces else context.workspace.name

        items = listed(pref, addon_tabs)
        count = len(items)
        max_page = max(0, (count - 1) // PAGE_SIZE)
        page = min(pref.page, max_page)
        if pref.page != page:
            # Avoid writing RNA properties during Panel.draw().
            pass
        r = layout.row()
        r.label(text=(f'{count} аддонов | Страница {page+1}/{max_page+1}' if pref.ui_language == 'RU' else f'{count} add-ons | Page {page+1}/{max_page+1}'))
        for mid, data, fav in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            box = layout.box()
            rec = pref.entries.get(mid)
            expanded = bool(rec and rec.expanded)
            head = box.row(align=True)
            if mid == SELF_ID:
                head.label(text='', icon='LOCKED')
            else:
                op = head.operator('ave_addons.expand', text='',
                                   icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT', emboss=False)
                op.module = mid
            display = data['name']
            if len(display) > 30:
                display = display[:27] + '...'
            head.label(text=display)
            op = head.operator('ave_addons.favorite', text='',
                               icon='SOLO_ON' if fav else 'SOLO_OFF', emboss=False)
            op.module = mid
            if not expanded or mid == SELF_ID:
                continue
            categories = sorted(addon_tabs.get(mid, set()), key=str.casefold)
            box.label(text=('N-вкладки: ' if pref.ui_language == 'RU' else 'N tabs: ') + ', '.join(categories[:4]), icon='WINDOW')
            if len(categories) > 4:
                box.label(text=(f'И ещё {len(categories)-4}' if pref.ui_language == 'RU' else f'And {len(categories)-4} more'))
            if mid in CONFLICTS:
                alert = box.column(align=True)
                alert.alert = True
                alert.label(text=tr('Panel conflict: native mode kept'), icon='ERROR')
                alert.label(text=CONFLICTS[mid][:55])
                op = alert.operator('ave_addons.force_sync', text=tr('Force synchronize'), icon='FILE_REFRESH')
                op.module = mid
            box.prop(rec, 'native_behavior', text=tr('Use original addon behavior'))
            settings = box.column()
            settings.enabled = not rec.native_behavior
            # Mode condition (existing working mode implementation).
            mode_box = settings.column(align=True)
            mode_box.label(text=tr('Show in mode:'), icon='EDITMODE_HLT')
            mode_box.prop(rec, 'n_mode', text='')
            # Workspace checkboxes are independent for every installed workspace.
            settings.separator()
            settings.label(text=tr('Visible in workspaces:'), icon='WINDOW')
            try:
                hidden = set(json.loads(rec.hidden_workspaces))
            except (ValueError, TypeError):
                hidden = set()
            for ws in bpy.data.workspaces:
                name = ws.name
                visible = name not in hidden
                row = settings.row(align=True)
                op = row.operator('ave_addons.workspace_toggle', text='',
                                  icon='CHECKBOX_HLT' if visible else 'CHECKBOX_DEHLT',
                                  emboss=False, depress=visible)
                op.module = mid
                op.workspace = name
                row.label(text=name)
            settings.label(text=tr('Enable workspace/mode filters above'), icon='INFO')
        if not items:
            layout.label(text=tr('Nothing found. Change filters.'), icon='INFO')
        r = layout.row(align=True)
        r.enabled = page > 0
        op = r.operator('ave_addons.page', text=tr('Previous'), icon='TRIA_LEFT')
        op.delta = -1
        r = layout.row(align=True)
        r.enabled = page < max_page
        op = r.operator('ave_addons.page', text=tr('Next'), icon='TRIA_RIGHT')
        op.delta = 1
        layout.label(text=tr('Only active N-sidebar add-ons are listed.'), icon='INFO')
        layout.label(text=tr('Save Blender preferences after changes.'), icon='INFO')
        footer = layout.box()
        footer.prop(pref, 'show_author', text=tr('Author / Support'), icon='URL', emboss=False)
        if pref.show_author:
            footer.operator('ave_addons.open_boosty', text=tr('Support development on Boosty'), icon='URL')


CLASSES = (
    AVE_AddonEntry, AVE_Profile, AVE_AddonPreferences,
    AVE_OT_toggle, AVE_OT_expand, AVE_OT_favorite, AVE_OT_workspace_select,
    AVE_OT_workspace_toggle, AVE_OT_workspace_show_all,
    AVE_OT_profile_save, AVE_OT_profile_select, AVE_OT_profile_apply,
    AVE_OT_profile_remove, AVE_OT_page, AVE_OT_mode_init, AVE_OT_resync_modes, AVE_OT_refresh,
    AVE_OT_save_settings, AVE_OT_backup, AVE_OT_open_boosty, AVE_OT_first_backup, AVE_OT_export, AVE_OT_import,
    AVE_OT_force_sync, AVE_OT_restore_all, AVE_OT_activate_session, AVE_PT_addons,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    # Delay scanning until Blender finishes registering any other startup add-ons.
    bpy.app.timers.register(_initial_backup_prompt, first_interval=2.5)


def _startup_sync():
    # Kept for compatibility with already scheduled timers from a prior install.
    return None


def _initial_backup_prompt():
    try:
        pref = prefs()
        if not pref.initial_backup_decided and not os.path.exists(_backup_path()):
            bpy.ops.ave_addons.first_backup('INVOKE_DEFAULT')
    except Exception as ex:
        print('AVE: initial backup prompt unavailable:', ex)
    return None


def unregister():
    if bpy.app.timers.is_registered(_initial_backup_prompt):
        bpy.app.timers.unregister(_initial_backup_prompt)
    if bpy.app.timers.is_registered(_deferred_sync):
        bpy.app.timers.unregister(_deferred_sync)
    restore_mode_rules()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
