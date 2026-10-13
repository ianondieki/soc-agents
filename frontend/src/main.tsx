import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, HashRouter } from "react-router-dom";
// IBM Plex is self-hosted (the NOC floor may be offline): the five Latin faces live in
// public/fonts, are declared at the top of styles.css and the Sans weights are preloaded in
// index.html, so no font waits behind the stylesheet and none is fetched from a CDN.
import "./styles.css";
import App from "./App";

const root = ReactDOM.createRoot(document.getElementById("root")!);

if (import.meta.env.VITE_DEMO === "1") {
  // The public demo build (`npm run build:demo`, docs/DEMO_BUILD.md): no server, every API answer
  // replayed from a recording of the real backend. Hash routes, because the page may be served
  // from any path. A normal build never includes src/demo.
  Promise.all([import("./demo/install"), import("./demo/DemoBar")]).then(async ([demo, bar]) => {
    try {
      await demo.installDemo();
    } catch (e) {
      root.render(<p style={{ padding: 24 }}>The demo could not start: {String((e as Error)?.message || e)} Reload the page to try again.</p>);
      return;
    }
    const DemoBar = bar.default;
    root.render(
      <React.StrictMode>
        <HashRouter>
          <DemoBar />
          <App />
        </HashRouter>
      </React.StrictMode>
    );
  });
} else {
  root.render(
    <React.StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </React.StrictMode>
  );
}
