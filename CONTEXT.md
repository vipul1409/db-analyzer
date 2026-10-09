# DB Analyzer

A conversational agent that explores a database through a read-only login and reports on storage, slow queries, index advice and entity hotspots.

## Language

### Targets and analysis

**Connection**:
A target the user configures, pointing at exactly one database.
_Avoid_: Database (for the configured target), server, DSN

**Capability**:
Something the analyzer can do on a Connection, such as measuring storage or reading the workload, given its store type, extensions and privileges. The agent is offered a tool only for capabilities the Connection has.
_Avoid_: Feature, permission

**Auxiliary session**:
An internal session a Connection opens to a second database it needs (e.g. `azure_sys` for Query Store), using the Connection's credentials and limits.
_Avoid_: Second connection

**Run**:
One execution of one or more analyzers against a Connection over a recorded **scope** (which analyzers, which collections were actually measured), broad or targeted. A follow-up that only explains data already collected is not a Run.
_Avoid_: Analysis (as a noun for the record), scan, turn

**Skipped collection**:
A collection a Run planned to measure but did not (gate rejection, missing privilege). It is outside the Run's scope and makes the Run partial.
_Avoid_: Failed table, excluded table

**Thread**:
A conversation with the agent, bound to one Connection for its whole life.
_Avoid_: Session, chat (as the stored record)

**Turn**:
One user message in a Thread and everything the agent does to answer it. A Turn may start Runs, or only explain data already collected.
_Avoid_: Run (a Turn is a conversational step, not an analysis record), request

**Finding**:
A problem or fact worth reporting about one subject (a collection, index, query or entity), with an identity (its **fingerprint**: category + subject) that stays stable across Runs.
_Avoid_: Issue, alert, recommendation

**Obsolete finding**:
A Finding whose subject can no longer exist (dropped object, entity key removed from the entity map, hash secret rotated). Distinct from a fixed Finding, which still exists but was resolved.
_Avoid_: Stale finding, closed

**Observation**:
What one Run saw for one Finding: its severity, evidence and recommendation at that time.
_Avoid_: Finding instance, result

### Entities and hotspots

**Entity type**:
A kind of business thing the schema models, such as Tenant, Account or Booking.
_Avoid_: Resource, resource type

**Entity key**:
A column that identifies an entity type in a given table, e.g. `bookings.tenant_id` for Tenant.
_Avoid_: Entity column, resource column, tenant column

**Entity**:
One instance of an entity type, e.g. tenant 4821.
_Avoid_: Resource, customer (unless that is the entity type)

**Entity map**:
The user-confirmed set of entity keys for a Connection, plus the hierarchy between entity types (booking → account → tenant).
_Avoid_: Resource map, tenant map

**Hotspot**:
An entity that holds a disproportionate share of rows or bytes in one or more collections.
_Avoid_: Heavy tenant, noisy neighbour

### Safety

**Guard profile**:
The set of statements the guard accepts for a given SQL source: the **agent** profile for SQL the LLM wrote, the **internal** profile for vetted templates and verbatim workload text.
_Avoid_: Mode, permission level

**EXPLAIN gate**:
The check that rejects a statement that reads relation data when its planned cost or row counts exceed the limits. Catalog-only and settings statements are exempt by type.
_Avoid_: Cost check, complexity filter

**Query cap**:
The most statements the agent may send to the database in one Turn; reaching it ends the Turn's database work with a stated reason, as a guard against runaway loops.
_Avoid_: Rate limit, quota
