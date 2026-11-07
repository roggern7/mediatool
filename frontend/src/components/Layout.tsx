import { useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  Captions,
  Clapperboard,
  Download,
  Library,
  ListOrdered,
  Menu,
  Monitor,
  Moon,
  ScanSearch,
  Scissors,
  SlidersHorizontal,
  Settings,
  Sun,
  X,
} from "lucide-react";
import { api } from "../api/client";
import { DiskBanner } from "./DiskBanner";
import { navigate, ROUTES } from "../lib/router";
import { THEME_OPTIONS, useThemePref, type ThemePref } from "../lib/theme";
import { useQuery } from "../lib/useQuery";

// Fluxo de trabalho no topo; ajustes no rodapé da barra lateral.
const NAV = [
  { to: ROUTES.import, label: "Importar", icon: Download },
  { to: ROUTES.library, label: "Biblioteca", icon: Library },
  { to: ROUTES.edit, label: "Editar", icon: Scissors },
  { to: ROUTES.captions, label: "Edição CapCut", icon: Captions },
  { to: ROUTES.queue, label: "Fila", icon: ListOrdered },
  { to: ROUTES.profiles, label: "Análise de Perfis", icon: ScanSearch },
] as const;

const NAV_SETUP = [
  { to: ROUTES.presets, label: "Presets", icon: SlidersHorizontal },
  { to: ROUTES.settings, label: "Configurações", icon: Settings },
] as const;

export function Link({
  to,
  className,
  children,
  onNavigate,
  current,
}: {
  to: string;
  className?: string;
  children: ReactNode;
  onNavigate?: () => void;
  current?: boolean;
}) {
  return (
    <a
      href={to}
      className={className}
      aria-current={current ? "page" : undefined}
      onClick={(e) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        navigate(to);
        onNavigate?.();
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

const THEME_ICON: Record<ThemePref, typeof Moon> = { dark: Moon, light: Sun, system: Monitor };

/** Escuro (padrão), claro ou o do sistema; salvo neste navegador. */
function ThemeSwitch() {
  const [pref, setPref] = useThemePref();
  return (
    <div className="seg seg-sm theme-switch" role="radiogroup" aria-label="Tema da interface">
      {THEME_OPTIONS.map(({ value, label }) => {
        const Icon = THEME_ICON[value];
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={pref === value}
            className={pref === value ? "on" : undefined}
            title={label}
            onClick={() => setPref(value)}
          >
            <Icon size={14} strokeWidth={1.75} aria-hidden="true" />
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );
}

function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true">
        <Clapperboard size={16} strokeWidth={2} />
      </span>
      Media Tool
    </div>
  );
}

function NavLinks({ path, onNavigate }: { path: string; onNavigate?: () => void }) {
  const item = ({ to, label, icon: Icon }: (typeof NAV)[number] | (typeof NAV_SETUP)[number]) => (
    <Link key={to} to={to} current={path === to} onNavigate={onNavigate} className={`nav-item${path === to ? " active" : ""}`}>
      <Icon size={17} strokeWidth={1.75} aria-hidden="true" />
      {label}
    </Link>
  );
  return (
    <>
      {NAV.map(item)}
      <div className="nav-setup">{NAV_SETUP.map(item)}</div>
    </>
  );
}

export function Layout({ path, children }: { path: string; children: ReactNode }) {
  const health = useHealth();
  const offline = health.error !== null;
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Navegação principal">
        <Brand />
        <NavLinks path={path} />
        <div className="sidebar-foot">
          <ThemeSwitch />
          <HealthIndicator health={health} />
        </div>
      </nav>

      {/* Telas estreitas: barra superior com o menu em folha lateral. */}
      <header className="topbar">
        <Dialog.Root open={menuOpen} onOpenChange={setMenuOpen}>
          <Dialog.Trigger className="btn btn-ghost btn-icon" aria-label="Abrir menu">
            <Menu size={20} strokeWidth={1.75} aria-hidden="true" />
          </Dialog.Trigger>
          <Dialog.Portal>
            <Dialog.Overlay className="nav-sheet-overlay" />
            <Dialog.Content className="nav-sheet" aria-describedby={undefined}>
              <div className="nav-sheet-head">
                <Dialog.Title asChild>
                  <div>
                    <Brand />
                  </div>
                </Dialog.Title>
                <Dialog.Close className="btn btn-ghost btn-icon" aria-label="Fechar menu">
                  <X size={18} strokeWidth={1.75} aria-hidden="true" />
                </Dialog.Close>
              </div>
              <nav aria-label="Navegação principal" className="nav-sheet-links">
                <NavLinks path={path} onNavigate={() => setMenuOpen(false)} />
              </nav>
              <div className="sidebar-foot">
                <ThemeSwitch />
                <HealthIndicator health={health} />
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
        <Brand />
        <span className={`dot${offline ? " err" : health.data ? " ok" : ""}`} aria-hidden="true" />
      </header>

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
