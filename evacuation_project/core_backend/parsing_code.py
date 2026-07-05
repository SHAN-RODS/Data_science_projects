import ifcopenshell    #importing the open-sourced Building Information Model library
import ifcopenshell.util.element as util  #Here, util is the property set module within ifcopenshell 

#Loading the residential building IFC model
ifc_file= r"C:\Users\Shannan\Desktop\Msc data science uog\term 3- msc project\bim residential models\ARK_NordicLCA_Housing_Concrete_As-Built_Revit-IFC4X3 original.ifc"
building_model= ifcopenshell.open(ifc_file)


#model.by_type("EntityName")   - extracting by IFC entity type


#examples 
spaces= building_model.by_type("IfcSpace")
doors= building_model.by_type("IfcDoor")
exits= building_model.by_type("IfcSpace")
stairs=building_model.by_type("IfcStair")
windows=building_model.by_type("IfcWindow")
corridors=building_model.by_type("IfcSpace")
walls=building_model.by_type("IfcWall")
slabs=building_model.by_type("IfcSlab")
space_boundaries= building_model.by_type("IfcRelSpaceBoundary")
total_elements_connected= building_model.by_type("IfcRelConnectsElements")
total_elevators= building_model.by_type("IfcTransportElement") 


def load_project_name():
    project= building_model.by_type("IfcProject")

    if project:
        return project[0].Name
    return "No IFC project name found."

def space_extract():

    total_rooms=[]

    for room in building_model.by_type("IfcSpace"):
        total_rooms.append({
            "id": room.GlobalId,
            "name": room.Name or "No Space name present"
        })
    return total_rooms

def each_corridors():

    total_corridors=[]

    for corridors in building_model.by_type("IfcSpace"):

        if "corridor" in corridors or "hallway" in corridors or "passage" in corridors:
            total_corridors.append({
                "id": corridors.GlobalID, 
                "name": corridors.Name

            })

    return total_corridors

def doors():

    all_doors=[]

    for door in building_model.by_type("IfcDoor"):

        width= door.OverallWidth 

        all_doors.append({
            "id": door.GlobalId,
            "name": door.Name 
            "width_metres": float(width) if width else None,
            "is_emergency_exit" : check_exit_present(door.Name, psets)



        })



def stairs():


def windows():


def walls():


def slabs():


def space_boundaries():


def connected_elements():


def transport_elements():






def summary_ifc_data():
    

parsed_building_elements= {"spaces": space_extract(),
                           "corridors": each_corridors()}
print(parsed_building_elements)

#every ifc object has basic attributes

#Some information is stored as IFC attributes, others are stored in Property sets

#Extract only what is needed 

#Build one dictionary 

