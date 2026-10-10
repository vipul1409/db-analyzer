import "@fontsource-variable/archivo/wdth.css";
import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/source-sans-3";
import "./styles/tokens.css";
import "./styles/app.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { Shell } from "./views/Shell";
import { ConnectionsPage } from "./views/ConnectionsPage";
import { ConnectionLayout } from "./views/ConnectionLayout";
import { ConnectionPage } from "./views/ConnectionPage";
import { ChatPage } from "./views/ChatPage";
import { RunsPage } from "./views/RunsPage";
import { RunPage } from "./views/RunPage";
import { ComparePage } from "./views/ComparePage";
import { FindingsPage } from "./views/FindingsPage";
import { AuditPage } from "./views/AuditPage";
import { UsagePage } from "./views/UsagePage";

const router = createBrowserRouter([
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <ConnectionsPage /> },
      { path: "usage", element: <UsagePage /> },
      {
        path: "c/:connectionId",
        element: <ConnectionLayout />,
        children: [
          { index: true, element: <ConnectionPage /> },
          { path: "chat", element: <ChatPage /> },
          { path: "chat/:threadId", element: <ChatPage /> },
          { path: "runs", element: <RunsPage /> },
          { path: "runs/compare", element: <ComparePage /> },
          { path: "runs/:runId", element: <RunPage /> },
          { path: "findings", element: <FindingsPage /> },
          { path: "audit", element: <AuditPage /> },
        ],
      },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
