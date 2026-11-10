## dump_netlist

### JsonConfig
```Json
{
  "briefInfo": "dump netlist",
  "description": "Write gate level netlist to the specified file",
  "isAtLeastOneArg": true,
}
```
### Argument

**-file**

```Json
{
  "token": ["-file"],
  "description": "[-file file_name]  (The output file name)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [1,1],
  "orderNum": 1
}
```


- Description:
  The output file name.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
-file <*file_name*>
file_name := <string>
```
**-pg**

```Json
{
  "token": ["-pg"],
  "description": "[-pg]  (This flag means to write power or ground pin/net/port to Verilog file)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write power or ground pin/net/port to Verilog file.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-pg]
```

**-param**

```Json
{
  "token": ["-param"],
  "description": "[-param]  (This flag means to write the parameters by function type of RTL to Verilog file)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write the parameters by function type of RTL to Verilog file.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-param]
```
**-check_hier**

```Json
{
  "token": ["-check_hier"],
  "description": "[-check_hier]  (This flag means to check whether the hierarchy of modules is legal)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to check whether the hierarchy of modules is legal.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-check_hier]
```
**-rtl**

```Json
{
  "token": ["-rtl"],
  "description": "[-rtl]  (This flag means to write syntax of RTL to Verilog file)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write syntax of RTL to Verilog file.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-rtl]
```
**-dfx**

```Json
{
  "token": ["-dfx"],
  "description": "[-dfx]  (This flag means to write all attributes and SDC information of the netlist object)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write all attributes and SDC information of the netlist object.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-dfx]
```

**-interface**

```Json
{
  "token": ["-interface"],
  "description": "[-interface]  (This flag means only write present module's ports and port declarations)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```

- Description: This flag means only write present module's ports and port declarations.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-interface]
```

**-no_datapath_module**

```Json
{
  "token": ["-no_datapath_module"],
  "description": "[-no_datapath_module]  (This flag means not to write the module which has been set for datapath when writing RTL syntax)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means not to write the module which has been set for datapath when writing RTL syntax.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-no_datapath_module]
```
**-write_x_net**

```Json
{
  "token": ["-write_x_net"],
  "description": "[-write_x_net]  (This flag means to write kX net assign)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write kX net assign.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

**-floating_pg**

```Json
{
  "token": ["-floating_pg"],
  "description": "[-floating_pg]  (This flag means to write floating power or ground pin/port to Verilog file)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write floating power or ground pin/port to Verilog file.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-floating_pg]
```
**-dp_point**

```Json
{
  "token": ["-dp_point"],
  "description": "[-dp_point dp_point]  (Specifies the index of the checkpoint module array to write the specified module to Verilog file)",
  "argType": "uint",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies the index of the checkpoint module array to write the specified module to Verilog file.
- Valid Value: An integer with values greater than or equal to 0.
- Default Value: N/A
- Rule: None

- Syntax
```
[-dp_point <*dp_point*>]
dp_point := <int>
```
**-write_isolate_net**

```Json
{
  "token": ["-write_isolate_net"],
  "description": "[-write_isolate_net]  (This flag means to write isolated single bit net or scalar net to Verilog file)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write isolated single bit net or scalar net to Verilog file.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-write_isolate_net]
```
**-line_number**

```Json
{
  "token": ["-line_number"],
  "description": "[-line_number]  (This flag means to write Line Number Attribute to Verilog file)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write Line Number Attribute to Verilog file.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-line_number]
```
**-only_used_module**

```Json
{
  "token": ["-only_used_module"],
  "description": "[-only_used_module]  (This flag means to write modules only used by top module)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write modules only used by top module.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-only_used_module]
```
**-write_const_net**

```Json
{
  "token": ["-write_const_net"],
  "description": "[-write_const_net]  (This flag means to write a net which is assigned with 1'b0 or 1'b1)",
  "argType": "bool",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  This flag means to write a net which is assigned with 1'b0 or 1'b1.
- Valid Value: N/A
- Default Value: N/A
- Rule: None
- Syntax
```
[-write_const_net]
```
### Description

Writes the current netlist information to the specified file.

### Example

Scenario : The following example shows the basic usage of this directive.

```
dump_netlist -file "./outputs/out.v"
```

### See Also

[load_netlist](#load_netlist)
