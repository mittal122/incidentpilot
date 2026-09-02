# Third-party components

IncidentPilot's console, UI, integration layer, Auto-Heal agent, demo
lab, and documentation are original work. The backend builds on the
following MIT-licensed open-source components, vendored under
`vendor/` with their license files preserved as required:

| Component | Role in IncidentPilot | License |
|---|---|---|
| `vendor/robusta` | alert pipeline runner (detection, enrichment, sinks, playbooks) | MIT |
| `vendor/holmesgpt` | AI investigation engine behind IncidentChat | MIT |

Other dependencies (FastAPI, htmx, and the Python packages in
`console/requirements.txt`) are used under their respective licenses.
