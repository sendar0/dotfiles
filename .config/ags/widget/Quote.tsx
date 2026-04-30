import app from "ags/gtk4/app"
import { Astal, Gtk, Gdk } from "ags/gtk4"
import { readFile, writeFile, monitorFile } from "ags/file"
import { createState } from "ags"
import GLib from "gi://GLib"

const QUOTE_FILE = `${GLib.get_home_dir()}/.cache/quote.txt`

function loadQuote(): string {
  try {
    return readFile(QUOTE_FILE).trim()
  } catch {
    return ""
  }
}

export default function Quote(gdkmonitor: Gdk.Monitor) {
  if (!GLib.file_test(QUOTE_FILE, GLib.FileTest.EXISTS)) {
    try { writeFile(QUOTE_FILE, "") } catch { /* ignore */ }
  }

  const [text, setText] = createState(loadQuote())
  monitorFile(QUOTE_FILE, () => setText(loadQuote()))

  const { BOTTOM } = Astal.WindowAnchor

  return (
    <window
      visible
      name="quote"
      class="Quote"
      gdkmonitor={gdkmonitor}
      anchor={BOTTOM}
      marginBottom={48}
      application={app}
      $={(self) => {
        self.set_layer(Astal.Layer.BOTTOM)
        self.set_namespace("quote")
      }}
    >
      <label
        class="quote-label"
        label={text}
        wrap
        justify={Gtk.Justification.CENTER}
        maxWidthChars={70}
      />
    </window>
  )
}
