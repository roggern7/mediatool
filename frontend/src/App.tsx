import { useEffect } from "react";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/Toasts";
import { LEGACY_ROUTES, ROUTES, redirect, usePath } from "./lib/router";
import { ImportPage } from "./pages/ImportPage";
import { LibraryPage } from "./pages/LibraryPage";
import { EditPage } from "./pages/EditPage";
import { QueuePage } from "./pages/QueuePage";
import { ProfileAnalysisPage } from "./pages/ProfileAnalysisPage";
import { PresetsPage } from "./pages/PresetsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { PostTimesPage } from "./pages/PostTimesPage";
import { CapCutPage } from "./pages/CapCutPage";
import { Empty } from "./components/ui";

function normalize(path: string): string {
  return path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;
}

function Page({ path }: { path: string }) {
  switch (path) {
    case ROUTES.import:
      return <ImportPage />;
    case ROUTES.library:
      return <LibraryPage />;
    case ROUTES.edit:
      return <EditPage />;
    case ROUTES.captions:
      return <CapCutPage />;
    case ROUTES.queue:
      return <QueuePage />;
    case ROUTES.profiles:
      return <ProfileAnalysisPage />;
    case ROUTES.presets:
      return <PresetsPage />;
    case ROUTES.settings:
      return <SettingsPage />;
    case ROUTES.postTimes:
      return <PostTimesPage />;
    default: {
      const moved = LEGACY_ROUTES[path];
      if (moved) return <Redirect to={moved} />;
      return (
        <div className="page">
          <Empty title="Página não encontrada" />
        </div>
      );
    }
  }
}

function Redirect({ to }: { to: string }) {
  useEffect(() => redirect(to), [to]);
  return null;
}

export function App() {
  const path = normalize(usePath());
  return (
    <ToastProvider>
      <Layout path={path}>
        <Page path={path} />
      </Layout>
    </ToastProvider>
  );
}
