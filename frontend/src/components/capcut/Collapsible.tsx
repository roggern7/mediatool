import type { ReactNode, Ref } from "react";

/**
 * Seção recolhível da página (fora do fluxo principal). Controlada: `open` + `onToggle`. O conteúdo fica montado
 * mesmo recolhido (o editor de modelos não perde o que está sendo editado).
 */
export function Collapsible({
  title,
  open,
  onToggle,
  children,
  ref,
}: {
  title: string;
  open: boolean;
  onToggle: (open: boolean) => void;
  children: ReactNode;
  ref?: Ref<HTMLDetailsElement>;
}) {
  return (
    <details ref={ref} className="more section cc-collapse" open={open} onToggle={(e) => onToggle(e.currentTarget.open)}>
      <summary>
        <h2>{title}</h2>
      </summary>
      <div className="cc-collapse-body">{children}</div>
    </details>
  );
}
