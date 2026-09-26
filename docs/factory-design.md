# Factory interface direction

The primary audience is a production manager planning machine sequences, with focused workspaces for machine operators, quality checkers, and warehouse managers.

## Visual system

- Deep steel `#17384C`: navigation, headings, machinery outlines.
- Workshop white `#F4F7F9`: workspace background and breathing room.
- Technical blue `#4C829E`: connections and secondary controls.
- Production teal `#258778`: selected machines and available space.
- Inspection amber `#B5802F`: quality stages and reservations.
- Storage violet `#806398`: warehouse destinations and occupied racks.

Bahnschrift gives headings and machine identifiers the condensed, engineered character of equipment labels. Segoe UI keeps forms and paragraphs readable; both have system fallbacks and work offline.

## Layout

The factory floor is the main visual, with equipment drawings and a readable production path. Secondary controls sit in a compact toolbar. Use a persistent route strip while editing and a quiet details column.

```text
Navigation | Title and actions
           | Floor status / build route
           | Machine floor               | Step details
           | QC bench / warehouse racks  | Minutes and notes
           | Available warehouse addresses
           | Route sequence / save
```

The first draft used a large generic banner and identical machine cards. The refinement removes that banner, draws recognizable production equipment, and uses rack-like storage bays. Numbers represent actual route sequence only. Color identifies responsibility and availability, with text alongside it. Preserve keyboard focus, phone layouts, and reduced motion support. A saved route and an order reservation remain visibly distinct.
