/* Radix UI Select をベースにしたドロップダウン。
   旧自前実装と同じ API (value / onChange / options / size / className) を保ちつつ、
   アクセシビリティ・キーボード操作・ポジショニング (collision detect) を Radix に委譲。
   見た目は既存の .select-trigger / .select-popover / .select-option CSS を流用。 */
import * as RadixSelect from "@radix-ui/react-select";
import { CaretDown, Check } from "@phosphor-icons/react";

export interface SelectOption {
  value: string;
  label: string;
  hint?: string;
}

interface Props {
  value: string;
  onChange: (v: string) => void;
  options: SelectOption[];
  placeholder?: string;
  size?: "sm" | "md";
  className?: string;
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
}

export function Select({
  value, onChange, options, placeholder, size = "md", className,
  disabled, title, ariaLabel,
}: Props) {
  const sizeClass = size === "sm" ? "h-7 text-[11px] px-2" : "h-8 text-[12px] px-2.5";
  const selected = options.find((o) => o.value === value);

  return (
    <RadixSelect.Root value={value} onValueChange={onChange} disabled={disabled}>
      <RadixSelect.Trigger
        className={`select-trigger ${sizeClass} ${className ?? ""}`}
        title={title}
        aria-label={ariaLabel}
      >
        <span className="flex-1 truncate text-left">
          <RadixSelect.Value placeholder={placeholder ?? ""}>
            {selected?.label ?? placeholder ?? ""}
          </RadixSelect.Value>
        </span>
        <RadixSelect.Icon className="select-trigger-icon shrink-0 ml-1.5 text-(--t3)">
          <CaretDown size={9} weight="bold" />
        </RadixSelect.Icon>
      </RadixSelect.Trigger>

      <RadixSelect.Portal>
        <RadixSelect.Content
          position="popper"
          sideOffset={4}
          collisionPadding={8}
          className="select-popover"
        >
          <RadixSelect.Viewport>
            {options.map((o) => (
              <RadixSelect.Item
                key={o.value}
                value={o.value}
                className="select-option"
              >
                <RadixSelect.ItemText>
                  <span className="flex-1 truncate text-left">
                    <span>{o.label}</span>
                    {o.hint && (
                      <span className="ml-1.5 text-(--t4) text-[10px]">{o.hint}</span>
                    )}
                  </span>
                </RadixSelect.ItemText>
                <RadixSelect.ItemIndicator className="ml-2 shrink-0">
                  <Check size={10} weight="bold" />
                </RadixSelect.ItemIndicator>
              </RadixSelect.Item>
            ))}
          </RadixSelect.Viewport>
        </RadixSelect.Content>
      </RadixSelect.Portal>
    </RadixSelect.Root>
  );
}
