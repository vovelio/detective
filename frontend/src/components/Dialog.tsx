import { useEffect, useRef } from "react";

export interface Confirmation {
  title: string;
  text: string;
  confirm: string;
  run: () => Promise<void>;
}
export function ConfirmDialog({
  value,
  close,
  disabled,
}: {
  value: Confirmation;
  close: () => void;
  disabled: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className="confirm-dialog"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      aria-labelledby="confirmation-title"
    >
      <span className="eyebrow">Подтверждение действия</span>
      <h2 id="confirmation-title">{value.title}</h2>
      <p>{value.text}</p>
      <div className="dialog-actions">
        <button className="secondary" onClick={close}>
          Отмена
        </button>
        <button
          disabled={disabled}
          onClick={() => {
            close();
            void value.run();
          }}
        >
          {value.confirm}
        </button>
      </div>
    </dialog>
  );
}
