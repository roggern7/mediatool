import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/Toasts";
import { ROUTES, usePath } from "./lib/router";
import { ImportPage } from "./pages/ImportPage";
import { LibraryPage } from "./pages/LibraryPage";
import { EditPage } from "./pages/EditPage";
import { QueuePage } from "./pages/QueuePage";
import { PresetsPage } from "./pages/PresetsPage";
import { SettingsPage } from "./pages/SettingsPage";
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
    case ROUTES.queue:
      return <QueuePage />;
    case ROUTES.presets:
      return <PresetsPage />;
    case ROUTES.settings:
      return <SettingsPage />;
    default:
      return (
        <div className="page">
          <Empty title="Página não encontrada" />
        </div>
      );
  }
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
