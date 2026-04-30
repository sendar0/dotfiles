import app from "ags/gtk4/app"
import style from "./style.scss"
import Bar from "./widget/Bar"
import Quote from "./widget/Quote"

app.start({
  css: style,
  main() {
    const monitors = app.get_monitors()
    // monitors.map(Bar)
    monitors.map(Quote)
  },
})
