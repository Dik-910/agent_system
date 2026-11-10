## delete_port

### JsonConfig
```Json
{
  "briefInfo": "delete port",
  "description": "Delete ports in the current design",
  "isAtLeastOneArg": false,
}
```
### Argument

**port_list**

```Json
{
  "token": [],
  "description": "[port_list]  (A list of ports will be removed)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies a list of ports to remove from the current design. Each port name must exist in the current design.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
<*port_list*>
port_list := <list.string>
```
### Description

This command removes ports from the current design or its subdesign. Bused ports cannot be removed with delete_port. Also, scalar (single bit) ports that are components of a bused port cannot be removed. In this case, you must first remove the bused port.

### Example

Scenario : The following example deletes a port named G18.

```
delete_port G18
```

### See Also

[add_port](#add_port)

[get_obj_ports](#get_obj_ports)
