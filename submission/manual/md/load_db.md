## load_db

### JsonConfig
```Json
{
  "briefInfo": "load db",
  "description": "Load the liberty, sdc, upf, verilog, lef, def, RC and tech information from data_path",
  "isAtLeastOneArg": false,
}
```
### Argument

**-no_liberty**

```Json
{
  "token": ["-no_liberty"],
  "description": "[-no_liberty]  (Specifies whether the library needs to be loaded)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies whether the library needs to be loaded.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-no_liberty]
```
**-no_timing_constraint**

```Json
{
  "token": ["-no_timing_constraint"],
  "description": "[-no_timing_constraint]  (Specifies whether the sdc constraints needs to be loaded)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies whether the sdc constraints needs to be loaded.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-no_timing_constraint]
```
**-no_upf**

```Json
{
  "token": ["-no_upf"],
  "description": "[-no_upf]  (Specifies whether upf needs to be loaded)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies whether upf needs to be loaded.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-no_upf]
```
**-no_netlist**

```Json
{
  "token": ["-no_netlist"],
  "description": "[-no_netlist]  (Specifies the no_netlist option)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "debugOnly": true
}
```

- Description:
  Specifies the no_netlist option.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-no_netlist]
```
**-no_physical_data**

```Json
{
  "token": ["-no_physical_data"],
  "description": "[-no_physical_data]  (Specifies the no_physical_data option)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "debugOnly": true
}
```
**-no_std_cell**

```Json
{
  "token": ["-no_std_cell"],
  "description": "[-no_std_cell]  (Specifies whether remove std cell after loading file)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies whether remove std cell after loading file.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-no_std_cell]
```
**db_file**

```Json
{
  "token": [],
  "description": "[db_file]  (Specifies the db file path)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the db file path, which contains information such as libraries, sdc, upf, verilog, lef, def, etc.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*db_file*>
db_file := <string>
```
**-merge**

```Json
{
  "token": ["-merge"],
  "description": "[-merge]  (Specifies the db file will be merge in current_project current_design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the db file will be merge in current_project current_design
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-merge]
```
### Description

Load the library, sdc, upf, verilog, lef, def, RC and tech information from db file.

### Example

Scenario : The following example shows the basic usage of this directive.

```
load_db "./datas/data_path/"
```

### See Also

[save_db](#save_db)
