# Design references

Updated 10 Oct 2026. "Checked" means I saw a screenshot or the live app.
"From description" means the information comes from a catalogue or search, without seeing the screen.
Behance and Dribbble search did not return project pages with screens, so those sources are not counted.

| # | Product | Link | Status | What we take |
|---|---|---|---|---|
| 1 | Stats (open source) | https://github.com/exelban/stats | checked (popup screenshot) | module cards, small section headings in capitals, thin dividers, "key — value" rows on the right, one history line |
| 2 | iStat Menus 7 | https://bjango.com/mac/istatmenus/ | from description (MacRumors: https://macrumors.com/2024/07/31/istat-menus-7-0-brings-new-features/) | the panel opens from the menu item, history and cores separately, one accent colour |
| 3 | Mac Activity | https://github.com/bigtomcat6/mac-activity | from description | live metric cards in the popover, a mini network graph |
| 4 | MacPulse | https://github.com/daniel29348679/macpulse | from description | switching metrics on and off in the status bar |
| 5 | MenuBarMonitor | https://www.producthunt.com/p/menubarmonitor | from description | detailed cores and temperature in one popover |
| 6 | MiniGauge | https://hunted.space/dashboard/minigauge | from description | minimalism, under 2 MB, a translucent background |
| 7 | Net Bar | https://www.hunted.space/product/net-bar-track-network-cpu-more | from description | a reworked interface: cleaner, fewer borders |
| 8 | System Monitor | https://apps.apple.com/app/systemmonitor/id423368786 | from description (App Store) | thinner graph lines, minimal corner radii |
| 9 | ActivityLine | https://alternativeto.net/software/activityline/about/ | from description | a single-row panel, a hover hint |
| 10 | DaisyDisk | https://daisydiskapp.com/ | from description | a dominant visualisation, a restrained background, no extra panels |

Also catalogued: Sensei (https://cindori.com/sensei), OnyX (no link in the sources).

## Conclusions for the report window and the panel

- One heading and one navigation. Duplicate titles and two headers were removed.
- No technical footers or service lines. Developer labels do not appear in the user-facing report.
- Cards with large numbers, small captions, small section headings in capitals.
- One accent colour (blue). Green, yellow and red only for state.
- Day picker: a calendar, one report per day. Days with a report are marked, the others are unavailable.
