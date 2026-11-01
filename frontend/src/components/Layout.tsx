import type { ReactNode } from "react";
import { Download, Library, ListOrdered, ScanSearch, Scissors, SlidersHorizontal, Settings } from "lucide-react";
import { api } from "../api/client";
import { DiskBanner } from "./DiskBanner";
import { navigate, ROUTES } from "../lib/router";
import { useQuery } from "../lib/useQuery";

const NAV = [
  { to: ROUTES.import, label: "Importar", icon: Download },
  { to: ROUTES.library, label: "Biblioteca", icon: Library },
  { to: ROUTES.edit, label: "Editar", icon: Scissors },
  { to: ROUTES.queue, label: "Fila", icon: ListOrdered },
  { to: ROUTES.profiles, label: "Análise de Perfis", icon: ScanSearch },
  { to: ROUTES.presets, label: "Presets", icon: SlidersHorizontal },
  { to: ROUTES.settings, label: "Configurações", icon: Settings },
] as const;

export function Link({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  return (
    <a
      href={to}
      className={className}
      onClick={(e) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

type HealthQuery = ReturnType<typeof useHealth>;

function useHealth() {
  return useQuery("health", () => api.health(), 10_000);
}

function HealthIndicator({ health }: { health: HealthQuery }) {
  const known = health.data !== undefined || health.error !== null;
  const online = health.error === null && health.data !== undefined;
  const label = !known ? "Verificando backend" : online ? "Backend conectado" : "Backend desconectado";
  return (
    <div className="row small" title={online && health.data ? `Versão ${health.data.version}` : undefined}>
      <span className={`dot${known ? (online ? " ok" : " err") : ""}`} aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function Layout({ path, children }: { path: string; children: ReactNode }) {
  const health = useHealth();
  const offline = health.error !== null;
  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Navegação principal">
        <div className="brand">Media Tool</div>
        {NAV.map(({ to, label, icon: Icon }) => (
          <Link key={to} to={to} className={`nav-item${path === to ? " active" : ""}`}>
            <Icon size={15} strokeWidth={1.6} aria-hidden="true" />
            {label}
          </Link>
        ))}
        <div className="sidebar-foot">
          <HealthIndicator health={health} />
        </div>
      </nav>
      <main className="main">
        {offline ? (
          <div className="banner" role="alert">
            <span className="dot err" aria-hidden="true" />
            <span className="grow">Backend desconectado. Verifique se o Media Tool está rodando.</span>
            <button type="button" className="btn btn-sm" onClick={health.reload}>
              Tentar de novo
            </button>
          </div>
        ) : (
          <DiskBanner />
        )}
        {children}
      </main>
    </div>
  );
}
