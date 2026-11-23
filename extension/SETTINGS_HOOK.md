# Extension settings menu hook

Keep feature logic in `extension/`. The original main menu only consumes the
optional `bot.settings_menu_entries` dictionary. No extension imports belong in
the core menu.

Each entry is keyed by a stable, namespaced button custom ID and supplies:

- `label`: button label and embed field heading.
- `description`: category summary.
- `callback`: async callable accepting the interaction. It must acknowledge the
  interaction and enforce its own permissions.

Register entries in the owning cog's `cog_load`; remove only that cog's entry in
`cog_unload`. Buttons look up the current registration when clicked, so menus
created before unload do not call an unloaded contribution. Row 3 is reserved
for contributions (up to five buttons). The dictionary is empty/absent when no
optional modules contribute.

The extension registers `ext:settings`, opens an ephemeral editor, and uses the
original `PermissionManager` to require global-admin access on opening, component
interactions, and modal submissions. The old `/admin_setting` command remains a
compatibility entry point with the same authorization and validation.

Existing `ext.*` database keys remain unchanged. Optional role/channel IDs can be
cleared; whitelists accept JSON lists of positive IDs (`[]` removes the restriction).
Validation checks ID syntax/range, not whether the role/channel exists or is
assignable. All values are bot-wide, not guild-scoped.

The original `/settings` command still checks personal admin records. Its
role-only-admin entry limitation is a separate core issue; this integration does
not modify the original permission system or bootstrap. Role-only global admins
can still use `/admin_setting`.