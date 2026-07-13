import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";
import App from "./App.vue";
import "./styles/theme.css";
import "./stores/theme"; // applies the persisted theme before first paint

import DashboardPage from "./pages/DashboardPage.vue";
import RunControlPage from "./pages/RunControlPage.vue";
import DevicesPage from "./pages/DevicesPage.vue";
import RunsPage from "./pages/RunsPage.vue";
import PluginsPage from "./pages/PluginsPage.vue";
import LogsPage from "./pages/LogsPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", component: DashboardPage, meta: { title: "Dashboard" } },
    { path: "/run", component: RunControlPage, meta: { title: "Run Control" } },
    { path: "/devices", component: DevicesPage, meta: { title: "Devices" } },
    { path: "/runs", component: RunsPage, meta: { title: "Runs" } },
    { path: "/plugins", component: PluginsPage, meta: { title: "Plugins" } },
    { path: "/logs", component: LogsPage, meta: { title: "Logs" } },
    { path: "/settings", component: SettingsPage, meta: { title: "Settings" } },
  ],
});

createApp(App).use(router).mount("#app");
