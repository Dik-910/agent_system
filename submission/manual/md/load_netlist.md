## load_netlist

### JsonConfig

```Json
{
  "briefInfo": "load netlist",
  "description": "Reads in one or more design or library files in Verilog format",
  "isAtLeastOneArg": true,
}
```

### Argument

**-top**

```Json
{
  "token": ["-top"],
  "description": "[-top top_module_name]  (Specifies the top_module_name (not instantiated) which to set as top module of current design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```

- Description:
  Specifies the top_module_name which to set as top module of current design.
- Valid Value: string
- Default Value: N/A
- Rule: None
- Syntax
```
[-top <*top_module_name*>]
top_module_name := <string>
```

**-z**

```Json
{
  "token": ["-z"],
  "description": "[-z 0 | 1] (Specifies the index for the z-axis plane, starting from 0 representing the topmost layer, and incrementing sequentially)",
  "argType": "int",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies the index for the z-axis plane, starting from 0 representing the topmost layer, and incrementing sequentially.
- Valid Value: int
- Default Value: N/A
- Rule: None

- Syntax
```
[-z <*die_name*>]
die_name := <int>
```

**-strict**

```Json
{
  "token": ["-strict"],
  "description": "[-strict]  (Strictly check the errors and interrupt the process when reading Verilog files)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```

- Description:
  Strictly check the errors and directly interrupt the process when reading verilog files
- Valid Value: N/A
- Default Value: N/A
- Rule: None
- Syntax
```
[-strict]
```

**-interface**

```Json
{
  "token": ["-interface"],
  "description": "[-interface {module_name1 module_name2}]  (Specifies modules whose ports are read only)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0,
  "exclusive": 1
}
```

- Description:
  Specifies modules whose ports are read only
- Valid Value: list.string
- Default Value: N/A
- Rule: None
- Syntax
```
[-interface <*module list*>]
module list := <string>
```


**-merge**

```Json
{
  "token": ["-merge"],
  "description": "[-merge module_name]  (Specifies a module which is updated incrementally)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0,
  "exclusive": 1
}
```

- Description:
  Specifies a module which is updated incrementally
- Valid Value: string
- Default Value: N/A
- Rule: None
- Syntax
```
[-merge <*module_name*>]
module_name := <string>
```

**-tech**

```Json
{
  "token": ["-tech"],
  "description": "[-tech tech_name]  (Specifies tech which will be used in this netlist)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```

- Description:
  Specifies tech which will be used in this netlist
- Valid Value: string
- Default Value: N/A
- Rule: None
- Syntax
```
[-tech <*tech_name*>]
tech_name := <string>
```

**verilog_files**

```Json
{
  "token": [],
  "description": "[verilog_files]  (Specifies the name of one or more Verilog files(netlist) to be read)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```

- Description:
  The name of one or more Verilog files to read.
- Valid Value: string
- Default Value: N/A
- Rule: None
- Syntax
```
<*verilog_files*>
verilog_files := <string>
```


### Description

The load_netlist command reads the design information from the Verilog file into memory.

### Example

Scenario : The following example reads a verilog file named xxx.v.

```
load_netlist xxx.v -top top
```

### See Also

[dump_netlist](#dump_netlist)
