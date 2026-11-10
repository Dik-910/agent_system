## add_inst

### JsonConfig
```Json
{
  "briefInfo": "add inst",
  "description": "Creates leaf or hierarchical insts in the current design or its sub-designs",
  "isAtLeastOneArg": true,
}
```
### Argument

**name_list**

```Json
{
  "token": [],
  "description": "[name_list]  (The name of the inst being created)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the names of cells created in the current design. Each cell name must be unique in the current hierarchy.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*name_list*>
name_list := <string>
```
**reference_name**

```Json
{
  "token": [],
  "description": "[reference_name]  (The design or library inst that new insts reference)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the design that new cells reference. You must specify the reference_name unless you use the -logic option. You must specify one of reference_name and -logic, and they are mutually exclusive.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*reference_name*>
reference_name := <string>
```
**-hier**

```Json
{
  "token": ["-hier"],
  "description": "[-hier]  (The hierarchical option)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  If you want to create hierarchical cells, you must specify this option.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-hier]
```
**-logic**

```Json
{
  "token": ["-logic"],
  "description": "[-logic value]  (The new inst generates a logic 0 or logic 1 value)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies this option to generate a logic 0 or logic 1 cell. The value must be either 0 or 1. The option cannot be used with reference_name.
- Valid Value: 0 | 1
- Default Value: N/A
- Rule: None

- Syntax
```
[-logic <*value*>]
value := (0 | 1)
```
**-non_unique**

```Json
{
  "token": ["-non_unique"],
  "description": "[-non_unique]  (Whether to auto uniquify when create hierarchical inst)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specify whether to auto uniquify when create hierarchical inst.
- Valid Value: N/A
- Default Value: N/A
- Rule: None

- Syntax
```
[-non_unique]
```
**-only_physical**

```Json
{
  "token": ["-only_physical"],
  "description": "[-only_physical]  (Creates a new physical or physical-only inst using a reference from the physical library)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "debugOnly": true
}
```
### Description

This command creates new leaf cells or hierarchical cells in the current design or its sub_modules based on the name_list parameter.New cells must have reference designs such as library cells, module_design, and logic 0 and logic 1.

The created cells are unplaced. You can manually set the placement of cells by set_cell_position commands.

### Example

Scenario 1: The following example creates a cell named inst_1 under the subdesign corresponding to the H1 cell:

```
add_inst H1/inst_1 gscl45nm/AOI21X1
get_obj_insts H1/inst_1
```

Scenario 2: The following example is to create a hierarchical cell named H3 using a new design as references:

```
add_inst H3 new_design -hier
get_obj_insts H3
```

### See Also

[delete_inst](#delete_inst)

[get_obj_insts](#get_obj_insts)
