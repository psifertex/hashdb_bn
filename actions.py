from typing import List, Optional, Tuple

from binaryninja import BinaryReader, BinaryView, interaction
from binaryninja.enums import InstructionTextTokenType, TypeClass
from binaryninja.log import Logger
from binaryninja.settings import Settings, SettingsScope
from binaryninja.types import EnumerationBuilder, Type
from binaryninja.plugin import BackgroundTaskThread
from binaryninja.mainthread import execute_on_main_thread

from . import hashdb_api as api

logger = Logger(session_id=0, logger_name=__name__)

#--------------------------------------------------------------------------
# Set xor key
#--------------------------------------------------------------------------
def set_xor_key(context):
    """
    Set xor key from selection
    """
    bv = context.binaryView
    token = context.token.token
    if token and token.type == InstructionTextTokenType.IntegerToken:
        if token.text.startswith('-'):
            #Handle negatives later
            logger.log_warn("plugin does not currently handle negative values.")
            return
        xor_value = token.value
        Settings().set_integer("hashdb.xor_value", xor_value, SettingsScope.SettingsResourceScope)
        logger.log_info(f"XOR key set: {xor_value:#x}")
        return True
    else:
        logger.log_info(f"failed to set XOR key.")
        return False


#--------------------------------------------------------------------------
# Hash lookup
#--------------------------------------------------------------------------
def hash_lookup(context):
    """
    Lookup hash from highlighted text
    """
    bv = context.binaryView
    token = context.token.token
    hashdb_xor_value = Settings().get_integer_with_scope("hashdb.xor_value", bv, SettingsScope.SettingsResourceScope)[0]
    hashdb_algorithm = get_hash(bv)

    if hashdb_algorithm is None:
        logger.log_error('No hash selected.')
        return

    if token and token.type == InstructionTextTokenType.IntegerToken:
        if token.text.startswith('-'):
            #Handle negatives later
            logger.log_warn("plugin does not currently handle negative values.")
            return
        hash_value = token.value
        hash_value ^= hashdb_xor_value

        # Lookup hash
        try:
            hash_results = api.get_strings_from_hash(hashdb_algorithm, hash_value, api_url=Settings().get_string("hashdb.url"))
        except Exception as e:
            logger.log_error(f"API request failed: {e}")
            return
        hash_list = hash_results.get('hashes',[])
        if len(hash_list) == 0:
            logger.log_warn(f"No Hash found for {hex(hash_value)}")
            return
        elif len(hash_list) == 1:
            hash_string = hash_list[0].get('string',{})
        else:
            # Multiple hashes found
            # Allow the user to select the best match
            collisions = {}
            for string_match in hash_list:
                string_value = string_match.get('string','')
                if string_value.get('is_api',False):
                    collisions[string_value.get('api','')] = string_value
                else:
                    collisions[string_value.get('string','')] = string_value
            string_selection = interaction.get_choice_input("Select the best match: ", "String Selection", list(collisions.keys()))
            if string_selection is not None:
                selected_string = list(collisions.keys())[string_selection]
            else:
                # User cancelled, select the first one?
                selected_string = list(collisions.keys())[0]
            hash_string = collisions[selected_string]

        # Parse string from hash_string match
        if hash_string.get('is_api',False):
            string_value = hash_string.get('api','')
        else:
            string_value = hash_string.get('string','')

        logger.log_info(f"Hash match found: {string_value}")
        if hash_string.get('is_api',False):
            # If the hash is an API ask if the user wants to
            # import all of the hashes from the module and permutation
            modules = hash_string.get('modules',[])
            modules.sort()
            module_choice = interaction.get_choice_input(f"The hash for {string_value} is a module function.\n\nDo you want to import all function hashes from this module?","HashDB Bulk Import", modules)
            if module_choice is not None:
                module_name = modules[module_choice]
                if module_name != None:
                    try:
                        #TODO: Background thread
                        module_hash_list = api.get_module_hashes(module_name, hashdb_algorithm, hash_string.get('permutation',''), api_url=Settings().get_string("hashdb.url"))
                        # Parse hash and string from list into tuple list [(string,hash)]
                        hash_list = []
                        for function_entry in module_hash_list.get('hashes',[]):
                            # If xor is enabled we must convert the hashes
                            hash_list.append((function_entry.get('string',{}).get('api',''),hashdb_xor_value^function_entry.get('hash',0)))
                        # Add hashes to enum
                        #TODO: Add hashes for the module
                        logger.log_info(hash_list)
                        enum_name = Settings().get_string_with_scope("hashdb.enum_name", bv)[0]
                        add_enums(bv, enum_name, hash_list)
                        #enum_id = add_enums(ENUM_NAME, enum_list)
                        #if enum_id == None:
                            #idaapi.msg("ERROR: Unable to create or find enum: %s\n" % ENUM_NAME)
                        #else:
                            #idaapi.msg("Added %d hashes for module %s\n" % (len(enum_list),module_name))
                    except Exception as e:
                        logger.log_error(f"ERROR {e}")
                        return
                else:
                    logger.log_error("Invalid module name specified.")
    else:
        logger.log_error("Invalid hash selected.")
        return
    return


def change_hash(context):
    context.binaryView.remove_metadata("HASHDB_ALGORITHM")
    get_hash(context.binaryView)


#--------------------------------------------------------------------------
# Ask for a hash
#--------------------------------------------------------------------------
def get_hash(bv):
    hashdb_algorithm = Settings().get_string_with_scope("hashdb.algorithm", bv, SettingsScope.SettingsResourceScope)[0]

    if hashdb_algorithm is None:
        algorithms = api.get_algorithms(api_url=Settings().get_string("hashdb.url"))
        algorithms.sort()
        algorithm_choice = interaction.get_choice_input("Select an algorithm:", "Algorithms", algorithms)
        if algorithm_choice is not None:
            result = algorithms[algorithm_choice]
            Settings().set_string("hashdb.algorithm", result, SettingsScope.SettingsResourceScope)
            return result
        else:
            return None
    else:
        return hashdb_algorithm

#--------------------------------------------------------------------------
# Dynamic IAT hash scan
#--------------------------------------------------------------------------
def hash_scan(context):
    """
    Lookup hash from highlighted text
    """
    bv = context.binaryView

    hashdb_xor_value = Settings().get_integer_with_scope("hashdb.xor_value", bv, SettingsScope.SettingsResourceScope)[0]
    hashdb_algorithm = get_hash(bv)
    logger.log_info(f"outside: {hashdb_algorithm}")

    # If there is no algorithm give the user a chance to choose one
    if hashdb_algorithm == None:
        logger.log_error("You must select a hash to continue.")
        return
    try:
        br = BinaryReader(bv, bv.endianness)
        br.seek(context.address)
        while br.offset < (context.address + context.length):
            hash_value = br.read32()
            hash_value ^= hashdb_xor_value
            hash_results = api.get_strings_from_hash(hashdb_algorithm, hash_value, api_url=Settings().get_string("hashdb.url"))

            # Extract hash info from results
            hash_list = hash_results.get('hashes',[])
            if len(hash_list) == 0:
                # No hash found
                # Increment the counter and continue
                continue
            elif len(hash_list) == 1:
                hash_string = hash_list[0].get('string',{})
            else:
                collisions = {}
                for string_match in hash_list:
                    string_value = string.match.get('string', '')
                    if string_value.get('is_api', False):
                        collisions[string_value.get('api','')] = string_value
                    else:
                        collisions[string_value.get('string','')] = string_value
                hash_choice = interaction.get_choice_input("Select the best hash: ", "Hash Selection", collisions.keys())
                if hash_choice is not None:
                    hash_string = collisions.keys()[hash_choice]
                else:
                    # User cancelled, select the first one?
                    hash_string = collisions.keys()[0]

            # Parse string from hash_string match
            if hash_string.get('is_api',False):
                string_value = hash_string.get('api','')
            else:
                string_value = hash_string.get('string','')
            logger.log_info(f"Hash match found: {string_value}")
            # Add hash to enum
            # TODO
    except Exception as e:
        logger.log_error(f"ERROR: {e}")
        return
    return


#--------------------------------------------------------------------------
# Algorithm search function
#--------------------------------------------------------------------------
class HuntAlgorithmTask(BackgroundTaskThread):
    def __init__(self, context, hash_value):
        super().__init__(initial_progress_text="[HashDB] Algorithm hunt task starting...", can_cancel=False)
        self.context = context
        self.match_results = None
        self.hash_value = hash_value

    def run(self):
        self.task_fn(self.hash_value)
        execute_on_main_thread(self.callback_fn)

    def task_fn(self, hash_value):
        try:
            match_results = api.hunt_hash(hash_value, api_url=Settings().get_string("hashdb.url"))
            match_results.sort()
            self.match_results = match_results
        except Exception as e:
            logger.log_error(f"HashDB API request failed: {e}")
            return

    def callback_fn(self):
        if self.match_results is None or len(self.match_results) == 0:
            interaction.show_message_box("No Match", "No algorithms matched the hash.")
        else:
            msg = """The following algorithms contain a matching hash.
            Select an algorithm to set as the default for this binary."""
            choice = interaction.get_choice_input(msg, "Select a hash", self.match_results)
            Settings().set_string("hashdb.algorithm", self.match_results[choice], self.context.binaryView, SettingsScope.SettingsResourceScope)


def hunt_algorithm(context):
    bv = context.binaryView
    hashdb_xor_value = Settings().get_integer_with_scope("hashdb.xor_value", bv, SettingsScope.SettingsResourceScope)[0]

    # Get selected hash
    token = context.token.token
    if token and token.type == InstructionTextTokenType.IntegerToken:
        if token.text.startswith('-'):
            #Handle negatives later
            logger.log_warn("plugin does not currently handle negative values.")
            return
        hash_value = token.value
        hash_value ^= hashdb_xor_value
        HuntAlgorithmTask(context=context, hash_value=hash_value).start()
    else:
        logger.log_warn("This token does not look like a valid integer.")


#--------------------------------------------------------------------------
# Enum creation
#--------------------------------------------------------------------------
def add_enums(bv: BinaryView, enum_name: str, hash_list: List[Tuple[str, int]]) -> None:
    # TODO: Normalize enum names, and fix potentially invalid enum names

    existing_type = bv.types.get(enum_name)
    if existing_type is None:
        # Create a new enum
        with EnumerationBuilder.builder(bv, enum_name) as new_enum:
            for enum_value_name, enum_value in hash_list:
                new_enum.append(enum_value_name, enum_value)
    else:
        # Modify an existing enum
        if existing_type.type_class == TypeClass.EnumerationTypeClass:
            with Type.builder(bv, enum_name) as existing_enum:
                # In Binary Ninja, enumeration members are not guaranteed to be unique.
                # It is possible to have 2 different enum members
                # with exactly the same name and the same value.
                # Therefore, we must take care to _replace_ any existing enum member
                # with the same name as the enum member we would like to add,
                # rather than _appending_ a duplicate member with the same name.

                # Create a list of member names to use for lookup.
                # EnumerationBuilder.replace requires a member index as an argument,
                # so we must save the original member index as well.
                member_dict = {
                    member.name: idx for (idx, member) in enumerate(existing_enum.members)
                }

                for enum_value_name, enum_value in hash_list:
                    if enum_value_name in member_dict:
                        existing_enum.replace(
                            member_dict.get(enum_value_name), # original member idx
                            enum_value_name, # new name
                            enum_value, # new value
                        )
                        # TODO: It's possible here that the user would like to
                        # always ignore any duplicate enum members,
                        # rather than always replacing them.
                        # Consider how to handle this in the future.
                    else:
                        # Enum member with this name doesn't yet exist
                        existing_enum.append(
                            enum_value_name, # new name
                            enum_value, # new value
                        )
        else:
            logger.log_error(f"Enum values could not be added; a non-enum type with the name {enum_name} already exists.")