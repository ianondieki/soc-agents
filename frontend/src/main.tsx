import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
// IBM Plex is self-hosted (the NOC floor may be offline): the five Latin faces live in
// public/fonts, are declared at the top of styles.css and the Sans weights are preloaded in
// index.html, so no font waits behind the stylesheet and none is fetched from a CDN.
import "./styles.css";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
