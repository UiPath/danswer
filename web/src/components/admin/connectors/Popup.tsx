import { useRef, useState } from "react";

export interface PopupSpec {
  message: string;
  type: "success" | "error";
  // Optional undo affordance. When present, the popup renders an "Undo"
  // button next to the message; clicking it invokes onClick and dismisses
  // the popup. The popup also stays on screen longer when undoable
  // (default 4s → 6s) so the user has time to react.
  undo?: {
    label?: string; // defaults to "Undo"
    onClick: () => Promise<void> | void;
  };
}

export const Popup: React.FC<
  PopupSpec & { onUndo?: () => void; onDismiss?: () => void }
> = ({ message, type, undo, onUndo, onDismiss }) => (
  <div
    className={`fixed bottom-4 left-4 p-4 rounded-md shadow-lg text-white z-[100] flex items-center gap-3 ${
      type === "success" ? "bg-green-500" : "bg-error"
    }`}
  >
    <span>{message}</span>
    {undo && (
      <button
        type="button"
        className="
          ml-2 px-3 py-1 rounded
          bg-white/20 hover:bg-white/30
          text-white text-sm font-medium
          focus:outline-none focus:ring-2 focus:ring-white/50
        "
        onClick={async () => {
          // Run the user's undo handler. Errors are swallowed at this
          // boundary — the popup will dismiss either way, and the page
          // will re-render to show whatever state actually persisted.
          try {
            await undo.onClick();
          } catch {
            /* noop */
          }
          onUndo?.();
        }}
      >
        {undo.label ?? "Undo"}
      </button>
    )}
    {onDismiss && (
      <button
        type="button"
        aria-label="Dismiss"
        className="ml-1 text-white/80 hover:text-white text-lg leading-none"
        onClick={onDismiss}
      >
        ×
      </button>
    )}
  </div>
);

export const usePopup = () => {
  const [popup, setPopup] = useState<PopupSpec | null>(null);
  // using NodeJS.Timeout because setTimeout in NodeJS returns a different type than in browsers
  const timeoutRef = useRef<NodeJS.Timeout | null>(null);

  const setPopupWithExpiration = (popupSpec: PopupSpec | null) => {
    // Clear any previous timeout
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
    }

    setPopup(popupSpec);
    if (popupSpec) {
      // Undoable popups stay on screen a bit longer — users need time to
      // notice the affordance and click it. 6s vs 4s for plain toasts.
      const ms = popupSpec.undo ? 6000 : 4000;
      timeoutRef.current = setTimeout(() => {
        setPopup(null);
      }, ms);
    }
  };

  return {
    popup: popup && (
      <Popup
        {...popup}
        onUndo={() => setPopupWithExpiration(null)}
        onDismiss={() => setPopupWithExpiration(null)}
      />
    ),
    setPopup: setPopupWithExpiration,
  };
};
