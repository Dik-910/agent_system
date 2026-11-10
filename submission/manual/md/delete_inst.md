## delete_inst

### JsonConfig
```Json
{
  "briefInfo": "delete inst",
  "description": "Delete insts from the current design",
  "isAtLeastOneArg": false,
}
```
### Argument

**inst_list**

```Json
{
  "token": [],
  "description": "[inst_list]  (Specifies a list of insts to be removed from the current design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the list of cells to remove from the current design. -all and inst_list are mutually exclusive and cannot be specified at the same time
- Valid Value: list.string
- Default Value: N/A
- Rule: '-all' and 'inst_list' are exclusive.

- Syntax
```
<*inst_list*>
inst_list := <list.string>
```
**-all**

```Json
{
  "token": ["-all"],
  "description": "[-all]  (Removes all insts in the current design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Indicates that all cells will be removed from the current design. -all and inst_list are mutually exclusive and cannot be specified at the same time.
- Valid Value: N/A
- Default Value: N/A
- Rule: '-all' and 'inst_list' are exclusive.

- Syntax
```
[-all]
```
### Description

The delete_inst command deletes cells from the current design. As with all other netlist editing commands, for the delete_inst command to succeed, at least one of its arguments must succeed. If all arguments fails, the network list remains unchanged. The delete_inst command returns 1 on success and 0 on failure.

Each cell specified must be within the range; That is, on or below the current instance. The delete_inst command can be used on leaf cells or hierarchy cells. As the delete_inst command executes, it passes the echo to its cell. However, only the cells that are actually deleted are recorded in the change list.

### Example

Scenario : The following example removes the specified cell using remove cell.

```
get_obj_insts
delete_inst [get_obj_insts inst_3]
get_obj_insts
```

### See Also

[add_inst](#add_inst)

[get_obj_insts](#get_obj_insts)
