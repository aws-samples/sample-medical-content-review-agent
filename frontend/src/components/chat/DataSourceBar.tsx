// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
"use client";

interface ToolConfig {
  enabled: boolean;
  default_on: boolean;
}

interface ToolMeta {
  id: string;
  name: string;
  icon: string;
  description: string;
}

// Order matters — left-to-right render order of the chip row
const TOOLS: ToolMeta[] = [
  {
    id: "pubmed",
    name: "PubMed",
    icon: "🔬",
    description: "Peer-reviewed biomedical literature",
  },
  {
    id: "openfda",
    name: "OpenFDA",
    icon: "💊",
    description: "FDA drug label database",
  },
  {
    id: "clinicaltrials",
    name: "ClinicalTrials.gov",
    icon: "🏥",
    description: "Registered clinical studies",
  },
  {
    id: "nova",
    name: "Nova Web Search",
    icon: "🌐",
    description: "Grounded web search via Amazon Nova",
  },
];

interface DataSourceBarProps {
  toolsConfig: Record<string, ToolConfig>;
  enabledSources: Record<string, boolean>;
  onToggle: (id: string) => void;
}

export function DataSourceBar({
  toolsConfig,
  enabledSources,
  onToggle,
}: DataSourceBarProps) {
  const rows = TOOLS.filter((t) => toolsConfig[t.id]?.enabled !== false);
  if (rows.length === 0) return null;

  return (
    <div className="bg-white/5 rounded-2xl border border-white/10 p-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-sm font-semibold text-white">
            External data sources
          </h3>
          <p className="text-xs text-gray-400 mt-0.5">
            The external reviewer cross-checks claims against the enabled
            databases.
          </p>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        {rows.map((tool) => {
          const isOn = enabledSources[tool.id] === true;
          return (
            <button
              key={tool.id}
              type="button"
              onClick={() => onToggle(tool.id)}
              title={tool.description}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${
                isOn
                  ? "bg-indigo-500/20 text-indigo-100 border-indigo-400/60 shadow-sm"
                  : "bg-white/5 text-gray-400 border-white/10 hover:bg-white/10"
              }`}
            >
              <span className="text-sm leading-none">{tool.icon}</span>
              <span>{tool.name}</span>
              {isOn && (
                <svg
                  className="w-3 h-3 text-indigo-200"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={3}
                    d="M5 13l4 4L19 7"
                  />
                </svg>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
