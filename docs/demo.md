# Recording the README clip

The clip at the top of the README is a scripted Claude Code session. The
[VHS](https://github.com/charmbracelet/vhs) tape in `scripts/demo.tape` types
each prompt, waits for the expected result on screen, and renders the GIF:

```sh
brew install ttyd ffmpeg
go install github.com/charmbracelet/vhs@v0.11.0
~/go/bin/vhs scripts/demo.tape
```

VHS 0.12.0 records but writes no output file
([charmbracelet/vhs#787](https://github.com/charmbracelet/vhs/issues/787)),
so pin 0.11.0 until a fixed release ships.

The tape needs a registered dangerzone server (see the [README](../README.md#register))
and a trusted `~/dangerzone-demo` directory. On a new machine, run `claude`
there once, accept the trust prompt, and `/exit`; the tape cannot answer that
prompt on its own.

## What the tape does

1. Launches `claude` in `~/dangerzone-demo` with an empty catalog. Only the
   dangerzone tools and one `cat` command are pre-approved, so no permission
   prompts appear.
2. Asks for a `square` tool and a call with 7, and waits for `49`.
3. Asks for an edit to return the cube, and waits for `343`.
4. Runs `! cat dangerzone.tools.json` inside the session to show the saved
   definition.
5. Exits, relaunches `claude` in the same directory, and asks for a call to
   the existing tool with no changes. The second `343` shows the edited
   definition survived the restart.

Each wait has a 180 second timeout. Because the model's wording varies from
run to run, only the numbers and the catalog's `"source"` key are matched; if
a run produces an unexpected screen, rerun the tape. Claude Code's own status
lines, such as a usage-limit warning, also end up in the clip, so record from
a quiet session.

## Output

The GIF lands at `docs/demo.gif`. Change `Set FontSize`, `Width`, or
`Height` in the tape for a different size, and add `Set PlaybackSpeed 1.5`
if the clip runs past about 90 seconds. Keep the file under about 5 MB; for a
larger result, change `Output` to `docs/demo.mp4`, which GitHub also renders
inline.
