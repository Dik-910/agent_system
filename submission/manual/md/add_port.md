## add_port

### JsonConfig
```Json
{
  "briefInfo": "add port",
  "description": "Creates ports in the current design or its subdesign",
  "isAtLeastOneArg": false,
}
```
### Argument

**port_list**

```Json
{
  "token": [],
  "description": "[port_list]  (Names of ports created in the current design)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies names of ports created in the current design. Each port name must be unique within the current design.
- Valid Value: list
- Default Value: N/A
- Rule: None

- Syntax
```
<*port_list*>
port_list := <list>
```
**-direction**

```Json
{
  "token": ["-direction"],
  "description": "[-direction dir]  (The signal flow of the created port)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [1,1],
  "orderNum": 0
}
```


- Description:
  Specifies the signal flow of the created port. The possible values are in, out, or inout. The default is in.
- Valid Value: in | out | inout
- Default Value: in
- Rule: None

- Syntax
```
[-direction <*dir*>]
dir := (<u>in</u> | out | inout)
```
### Description

The add_port command creates new port objects in the current design or its subdesign. The add_port command creates only scalar or single bit ports. If create bus ports , use the add_bus command.

Ports are the external connection points on a design. To connect ports to nets inside a design, use the connect_net command. To remove ports from the current design, use the delete_port command.

### Example

Scenario 1: The following example uses add_port to create ports in the current design:

```
add_port {B1 B2 B3} -direction in
get_obj_ports B*
```

Scenario 2: The following example uses add_port to create ports in the MID subdesign. H1 is a unique instance of the design named H1.

```
add_port {H1/A1 H1/A2 H1/A3} -direction inout
get_obj_pins H1/A*
```

### See Also

[get_obj_ports](#get_obj_ports)

[delete_port](#delete_port)
