# Entity–Relationship Diagram for Solver Output Data

**Author**: Joshua Kempster

The diagram below displays a proposed schema for storing the solution output in a structured database.

**Design decisions**:

- No duplication of data: source, plant and other data that is input to the model is not included in the solution output. The ids are incldued and can be mapped back to the original tables.
- Sources, Plants and Demand Zone tables have composite primary keys that combine the solution_id and node id (e.g. source_id), so that a solution cannot be accidentally joined to the wrong sources. This is not represented in the diagram, which needs to be fixed, but it was causing rendering issues.
- A scenario can have multiple solutions (depending on the version of the model, or if a different solver is used), hence each solution has a unique id.
- The number of sources, plants, demand zones and quality measures can and will change per scenario, hence tables are long and joined, rather than being wide and requiring updates to schema if fields are added.

> [!warning] Note for data engineering team
> The exact schema is not finalised, but I do have DDL statements for each of the tables proposed below, so if you're happy with it, I can share that too.

```mermaid
erDiagram
    SCENARIOS ||--o{ SOLUTIONS : "has many"
    SOLUTIONS ||--o{ SOLUTION_SOURCES : includes
    SOLUTIONS ||--o{ SOLUTION_PLANTS : includes
    SOLUTIONS ||--o{ SOLUTION_DEMAND_ZONES : includes
    SOLUTIONS ||--o{ SOLUTION_SOURCE_TO_PLANT_FLOWS : includes
    SOLUTIONS ||--o{ SOLUTION_PLANT_TO_ZONE_FLOWS : includes
    SOLUTIONS ||--o{ SOLUTION_BLENDED_QUALITY_RESULTS : includes

    SOURCES ||--o{ SOLUTION_SOURCES : "master data for"
    PLANTS ||--o{ SOLUTION_PLANTS : "master data for"
    DEMAND_ZONES ||--o{ SOLUTION_DEMAND_ZONES : "master data for"

    SOLUTION_SOURCES ||--o{ SOLUTION_SOURCE_TO_PLANT_FLOWS : "flows from"
    SOLUTION_PLANTS ||--o{ SOLUTION_SOURCE_TO_PLANT_FLOWS : "flows into"
    SOLUTION_PLANTS ||--o{ SOLUTION_PLANT_TO_ZONE_FLOWS : "flows from"
    SOLUTION_DEMAND_ZONES ||--o{ SOLUTION_PLANT_TO_ZONE_FLOWS : "flows into"
    SOLUTION_PLANTS ||--o{ SOLUTION_BLENDED_QUALITY_RESULTS : "measured at"

    SOLUTIONS {
        text solution_id PK
        text scenario_id FK
        text solver_status
        numeric solver_objective_value
        numeric total_source_fixed_cost
        numeric total_source_withdrawal_cost
        numeric total_plant_fixed_cost
        numeric total_plant_treatment_cost
        numeric reconstructed_total_cost
        boolean cost_reconciles
        jsonb warnings
        jsonb notes
    }
    SOLUTION_SOURCES {
        text solution_id FK
        text source_id FK
        boolean is_selected
        numeric withdrawal_ml_per_day
        numeric blend_ratio
    }
    SOLUTION_PLANTS {
        text solution_id FK
        text plant_id FK
        boolean is_active
        numeric throughput_ml_per_day
    }
    SOLUTION_DEMAND_ZONES {
        text solution_id FK
        text zone_id FK
        numeric demand_ml_per_day
        numeric delivered_ml_per_day
    }
    SOLUTION_SOURCE_TO_PLANT_FLOWS {
        bigint flow_id PK
        text solution_id FK
        text source_id FK
        text plant_id FK
        numeric flow_ml_per_day
    }
    SOLUTION_PLANT_TO_ZONE_FLOWS {
        bigint flow_id PK
        text solution_id FK
        text plant_id FK
        text zone_id FK
        numeric flow_ml_per_day
    }
    SOLUTION_BLENDED_QUALITY_RESULTS {
        bigint result_id PK
        text solution_id FK
        text plant_id FK
        text parameter_id
        numeric blended_value
    }
```
