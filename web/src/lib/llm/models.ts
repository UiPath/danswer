export interface VendorOption {
  key: string;
  label: string;
}

export interface ModelOption {
  name: string;
  value: string;
}

export const LLM_VENDORS: VendorOption[] = [
  { key: "openai", label: "OpenAI (GPT)" },
  { key: "awsbedrock", label: "AWS Bedrock (Claude)" },
];

export const LLM_MODELS_BY_VENDOR: Record<string, ModelOption[]> = {
  openai: [
    { name: "GPT-4o (2024-11-20)", value: "gpt-4o-2024-11-20" },
    { name: "GPT-4.1 Mini", value: "gpt-4.1-mini-2025-04-14" },
  ],
  awsbedrock: [
    {
      name: "Claude Sonnet 4.5",
      value: "anthropic.claude-sonnet-4-5-20250929-v1:0",
    },
  ],
};

export interface FlatModelOption {
  vendorKey: string;
  vendorLabel: string;
  modelId: string;
  modelLabel: string;
}

export const FLAT_LLM_MODELS: FlatModelOption[] = LLM_VENDORS.flatMap((vendor) =>
  (LLM_MODELS_BY_VENDOR[vendor.key] ?? []).map((m) => ({
    vendorKey: vendor.key,
    vendorLabel: vendor.label,
    modelId: m.value,
    modelLabel: m.name,
  }))
);

export function getModelDisplayName(
  vendorKey: string | undefined,
  modelId: string | undefined
): string | null {
  if (!modelId) return null;
  const match = FLAT_LLM_MODELS.find(
    (m) =>
      m.modelId === modelId && (!vendorKey || m.vendorKey === vendorKey)
  );
  return match ? match.modelLabel : modelId;
}
