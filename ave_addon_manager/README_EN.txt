AVE ADDON MANAGER — 0.8.0 ALPHA
README / USER GUIDE

IMPORTANT: THIS IS A TEST ALPHA BUILD!
Bugs, missing tabs during a session, and incompatibilities with other add-ons are possible. Save your project and back up your Blender user configuration before testing. Do not use experimental features in critical production files. Please report any issues you encounter.

BUG REPORTS
Preferred channel: GitHub Issues — https://github.com/AveKven/AVE-Addon-Manager/issues
Alternatively, contact the author on Boosty: https://boosty.to/ave_blender

OVERVIEW
AVE Addon Manager helps organize the visibility of existing add-on tabs in the N-sidebar of Blender's 3D Viewport. Visibility can be controlled per Blender workspace (top tabs) and Object / Edit mode. It does not move add-on files and is not intended to modify other add-ons' own preferences.

FEATURES
• Lists enabled add-ons with detected N-sidebar panels; search and Favorites (star) to filter the list.
• Expandable add-on entries showing detected N-tabs, display modes, and checkboxes for existing Blender workspaces.
• Use original addon behavior: preserve an add-on's native behavior; enabled by default.
• Object / Edit and per-workspace rules take effect only when the corresponding filters are enabled and Apply rules for this session is pressed.
• Rules are inactive when Blender starts; session filtering requires explicit activation.
• Restore panels attempts to restore original panel registration and visibility logic.
• Original snapshot saves AVE configuration and detected panel metadata in JSON; it is NOT a full backup of third-party add-on preferences.
• Export JSON / Import JSON allows transferring AVE rules and Favorites (default export filename: AVE_Addons.json). Import does not install missing add-ons or activate imported rules automatically.
• Force synchronize retries a conflicting panel manually; use with caution and back up first.
• RU/EN interface switch: Edit → Preferences → Add-ons → AVE Addon Manager.
• Author page: https://boosty.to/ave_blender.

INSTALLATION
1. In Blender 5.2 LTS, open Edit → Preferences → Add-ons → Install from Disk.
2. Select the ZIP without extracting it, then enable AVE Addon Manager.
3. Open 3D Viewport → N → AVE Addons.
4. On first launch, accept the initial snapshot prompt if you want a record of AVE state.
5. For a chosen add-on, uncheck Use original addon behavior; select workspace/mode rules, enable the matching filters, and click Apply rules for this session.
6. Test one add-on at a time, and save Blender preferences if you want to retain your choices.

RESTORATION AND UNINSTALLATION
Prefer clicking Restore panels before uninstalling. On normal disable, AVE attempts to reverse its temporary changes. If a third-party tab does not return, fully restart Blender; if necessary, disable and re-enable the affected add-on. Universal compatibility cannot be guaranteed.

DATA AND LIMITATIONS
AVE is not intended to alter the source .py files of other add-ons. To filter visibility it temporarily interacts with the registration/visibility methods of some Python panels in Blender's running process, which can cause compatibility issues. An AVE JSON snapshot is not a substitute for backing up Blender's user configuration folder.

Author: AVE Blender
Boosty: https://boosty.to/ave_blender
