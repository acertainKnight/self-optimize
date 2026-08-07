---
name: widget-namer
description: Generates Widgetforge config-key names from a feature request. Synthetic demo skill for self-optimize -- see demo/README.md.
---
SYNTHETIC DEMO CONTENT. Widgetforge is a fictional product invented for this
demo. Nothing below is a real project, session, or user.

# widget-namer

Turns a feature request into a config key name for the Widgetforge integration
layer.

- Take the feature name the user gives you.
- Convert it to kebab-case: lowercase words joined with hyphens, never snake_case or CamelCase.
- Prefix the key with `wf-` and hand it back.
