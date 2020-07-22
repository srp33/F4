import datetime
import fastnumbers
import gzip
from joblib import Parallel, delayed
from Helper import *
import math
import os
from Parser import *
import shutil
import sys
import tempfile

def convert_delimited_file_to_f4(in_file_path, f4_file_path, in_file_delimiter="\t", index_columns=None, num_processes=1, num_cols_per_chunk=None, tmp_dir_path=None):
    if type(in_file_delimiter) != str:
        raise Exception("The in_file_delimiter value must be a string.")
    if in_file_delimiter not in ("\t"):
        raise Exception("Invalid delimiter. Must be \t.")

    __print_message(f"Parsing {in_file_path}")

    in_file_delimiter = in_file_delimiter.encode()

    # Get column names. Remove any leading or trailing white space around the column names.
    in_file = __get_delimited_file_handle(in_file_path)
    column_names = [x.strip() for x in in_file.readline().rstrip(b"\n").split(in_file_delimiter)]
    num_cols = len(column_names)
    in_file.close()

    if num_cols == 0:
        raise Exception(f"No data was detected in {in_file_path}.")

    if __should_index(index_columns, num_cols):
        # We perform this here so we can check early whether the index column names are valid.
        parser = Parser(f4_file_path)
        index_columns = [x.encode() for x in index_columns]
        index_column_indices = parser.get_column_indices(index_columns)

    column_chunk_indices = __generate_chunk_ranges(num_cols, num_cols_per_chunk)

    # Iterate through the lines to find the max width of each column.
    __print_message(f"Finding max width of each column")
    chunk_results = Parallel(n_jobs=num_processes)(delayed(__parse_columns_chunk)(in_file_path, in_file_delimiter, column_chunk[0], column_chunk[1]) for column_chunk in column_chunk_indices)

    # Summarize the column sizes and types across the chunks.
    column_sizes = []
    column_types = []
    for chunk_tuple in chunk_results:
        for i, size in sorted(chunk_tuple[0].items()):
            column_sizes.append(size)
        for i, the_type in sorted(chunk_tuple[1].items()):
            column_types.append(the_type)

    num_rows = chunk_results[0][2]

    if num_rows == 0:
        raise Exception(f"A header rows but no data rows were detected in {in_file_path}")

    # Calculate and save the column coordinates and max length of these coordinates.
    column_start_coords = __get_column_start_coords(column_sizes)
    column_coords_string, max_column_coord_length = __build_string_map(column_start_coords)
    __write_string_to_file(f4_file_path, ".cc", column_coords_string)
    __write_string_to_file(f4_file_path, ".mccl", str(max_column_coord_length).encode())

    # Build a map of the column names and save this to a file.
    column_names_string, max_col_name_length = __build_string_map(column_names)
    __write_string_to_file(f4_file_path, ".cn", column_names_string)
    __write_string_to_file(f4_file_path, ".mcnl", str(max_col_name_length).encode())

    # Save number of rows and cols.
    __write_string_to_file(f4_file_path, ".nrow", str(num_rows).encode())
    __write_string_to_file(f4_file_path, ".ncol", str(len(column_names)).encode())

    # Build a map of the column types and save this to a file.
    column_types_string, max_col_type_length = __build_string_map(column_types)
    __write_string_to_file(f4_file_path, ".ct", column_types_string)
    #__write_string_to_file(f4_file_path, ".mctl", str(max_col_type_length).encode())

    # Figure out where temp files will be stored.
    if tmp_dir_path:
        if not os.path.exists(tmp_dir_path):
            os.makedirs(tmp_dir_path)
    else:
        tmp_dir_path = tempfile.mkdtemp()
    if not tmp_dir_path.endswith("/"):
        tmp_dir_path += "/"

    __print_message(f"Parsing chunks of the input file and saving to temp files")
    row_chunk_indices = __generate_chunk_ranges(num_rows, math.ceil(num_rows / num_processes) + 1)
    max_line_sizes = Parallel(n_jobs=num_processes)(delayed(__save_rows_chunk)(in_file_path, in_file_delimiter, column_sizes, tmp_dir_path, i, row_chunk[0], row_chunk[1]) for i, row_chunk in enumerate(row_chunk_indices))

    # Find and save the line length.
    #TODO: Update this when you enable compression?
    line_length = max(max_line_sizes)
    __write_string_to_file(f4_file_path, ".ll", str(line_length).encode())

    # Merge the file chunks. This dictionary enables us to sort them properly.
    __print_message(f"Merging the file chunks")
    __merge_chunk_files(f4_file_path, tmp_dir_path, num_processes)

    # Remove the temp directory if it was generated by the code (not the user).
    if tmp_dir_path:
        try:
            os.rmdir(tmp_dir_path)
        except:
            # Don't throw an exception if we can't delete the directory.
            pass

    __print_message(f"Done saving to {f4_file_path}")

    # Save index column data if they are a subset of the full column set and if we are doing compression.
    if __should_index(index_columns, num_cols):
        __save_index(parser, index_columns, index_column_indices, num_rows)
        parser.close()

def __should_index(index_columns, num_cols):
    return index_columns and len(index_columns) < num_cols

#####################################################
# Private functions
#####################################################

def __get_delimited_file_handle(file_path):
    if file_path.endswith(".gz"):
        return gzip.open(file_path)
    else:
        return open(file_path, 'rb')

def __get_column_start_coords(column_sizes):
    # Calculate the position where each column starts.
    column_start_coords = []
    cumulative_position = 0
    for column_size in column_sizes:
        column_start_coords.append(str(cumulative_position).encode())
        cumulative_position += column_size
    column_start_coords.append(str(cumulative_position).encode())

    return column_start_coords

def __generate_chunk_ranges(num_cols, num_cols_per_chunk):
    if num_cols_per_chunk:
        last_end_index = 0

        while last_end_index != num_cols:
            last_start_index = last_end_index
            last_end_index = min([last_start_index + num_cols_per_chunk, num_cols])
            yield [last_start_index, last_end_index]
    else:
        yield [0, num_cols]

def __parse_columns_chunk(file_path, delimiter, start_index, end_index):
    in_file = __get_delimited_file_handle(file_path)

    # Ignore the header line because we don't need column names here.
    in_file.readline()

    # Initialize the column sizes and types.
    column_sizes_dict = {}
    column_types_dict = {}
    for i in range(start_index, end_index):
        column_sizes_dict[i] = 0
        column_types_dict[i] = None

    # Loop through the file for the specified columns.
    num_rows = 0
    for line in in_file:
        line_items = line.rstrip(b"\n").split(delimiter)
        for i in range(start_index, end_index):
            column_sizes_dict[i] = max([column_sizes_dict[i], len(line_items[i])])
            column_types_dict[i] = __infer_type_from_list([column_types_dict[i], __infer_type(line_items[i])])

        num_rows += 1

    in_file.close()

    return column_sizes_dict, column_types_dict, num_rows

def __save_rows_chunk(file_path, delimiter, column_sizes, tmp_dir_path, chunk_number, start_index, end_index):
    max_line_size = 0

    # Save the data to output file. Ignore the header line.
    in_file = __get_delimited_file_handle(file_path)
    in_file.readline()

    with open(f"{tmp_dir_path}{chunk_number}", 'wb') as tmp_file:
        line_index = -1
        for line in in_file:
            # Check whether we should process the specified line.
            line_index += 1
            if line_index < start_index:
                continue
            if line_index == end_index:
                break

            # Parse the data from the input file.
            line_items = line.rstrip(b"\n").split(delimiter)

            # Format the data using fixed widths and save to a temp file.
            out_items = [__format_string_as_fixed_width(line_items[i], size) for i, size in enumerate(column_sizes)]
            out_line = b"".join(out_items) + b"\n"
            tmp_file.write(out_line)

            # Update the maximum line size based on this line.
            max_line_size = max([max_line_size, len(out_line)])

    in_file.close()

    return max_line_size

def __merge_chunk_files(f4_file_path, tmp_dir_path, num_processes, lines_per_chunk=10):
    if num_processes == 1:
        # We don't need to merge because there is only one file.
        shutil.move(f"{tmp_dir_path}0", f4_file_path)
    else:
        with open(f4_file_path, "wb") as f4_file:
            out_lines = []

            for i in range(num_processes):
                chunk_file_path = f"{tmp_dir_path}{i}"
                if not os.path.exists(chunk_file_path):
                    continue

                with open(chunk_file_path, "rb") as chunk_file:
                    for line in chunk_file:
                        out_lines.append(line)

                        if len(out_lines) % lines_per_chunk == 0:
                            f4_file.write(b"".join(out_lines))
                            out_lines = []

                os.remove(chunk_file_path)

            if len(out_lines) > 0:
                f4_file.write(b"".join(out_lines))

def __infer_type(value):
    if not value or is_missing_value(value):
        return None
    if fastnumbers.isint(value):
        return b"i"
    if fastnumbers.isfloat(value):
        return b"f"
    return b"c"

def __infer_type_from_list(types):
    # Remove any None or missing values. Convert to set so only contains unique values and is faster.
    types = [x for x in types if x and not is_missing_value(x)]

    if len(types) == 0:
        return None
    if len(types) == 1:
        return types[0]

    types = set(types)

    if b"c" in types:
        return b"c" # If any value is non-numeric, then we infer categorical.
    if b"f" in types:
        return b"f" # If any value if a float in a numeric column, then we infer float.
    return b"i"

def __format_string_as_fixed_width(x, size):
    return x + b" " * (size - len(x))

def __build_string_map(the_list):
    # Find maximum length of value.
    max_value_length = __get_max_string_length(the_list)

    # Build output string.
    output = ""
    formatter = "{:<" + str(max_value_length) + "}\n"
    for value in the_list:
        output += formatter.format(value.decode())

    return output.encode(), max_value_length

def __get_max_string_length(the_list):
    return max([len(x) for x in set(the_list)])

def __write_string_to_file(file_path, file_extension, the_string):
    with open(file_path + file_extension, 'wb') as the_file:
        the_file.write(the_string)

def __save_index(parser, index_columns, index_column_indices, num_rows):
    # Find coordinates of index columns.
    index_column_coords = parser._parse_data_coords(index_column_indices)

    # Store index column data.
    with open(f"{parser.data_file_path}.idx", 'wb') as index_file:
        for row_index in range(num_rows):
            row = b"".join(parser.parse_row_values(row_index, index_column_coords)) + b"\n"
            index_file.write(row)

    # Save the length of the last row (all rows will have the same length).
    __write_string_to_file(f"{parser.data_file_path}", ".idx.ll", str(len(row)).encode())

    # Indicate the number of index columns
    __write_string_to_file(f"{parser.data_file_path}", ".idx.ncol", str(len(index_columns)).encode())

    # Calculate and save the index column coordinates and max length of these coordinates.
    index_column_sizes = []
    for coord in index_column_coords:
        index_column_sizes.append(coord[1] - coord[0])
    index_column_start_coords = __get_column_start_coords(index_column_sizes)
    column_coords_string, max_column_coord_length = __build_string_map(index_column_start_coords)
    __write_string_to_file(parser.data_file_path, ".idx.cc", column_coords_string)
    __write_string_to_file(parser.data_file_path, ".idx.mccl", str(max_column_coord_length).encode())

#def __save_transposed_index_columns(f4_file_path, index_columns, index_column_indices, num_rows):
#    # Use a parser to work with index columns (will be transposed).
#    parser = Parser(f4_file_path)
#
#    # Find coordinates of index columns.
#    index_column_coords = parser._parse_data_coords(index_column_indices)
#
#    # Find width information for index columns.
#    column_widths = []
#
#    for i, column_index in enumerate(index_column_indices):
#        coords = index_column_coords[i]
#        column_width = coords[1] - coords[0]
#        column_widths.append(column_width)
#
#    max_line_length = max(column_widths) * num_rows
#
#    # Store index column data.
#    with open(f"{f4_file_path}.t", 'wb') as transposed_file:
#        for i, column_width in enumerate(column_widths):
#            # Merge values into a transposed string.
#            out_string = b"".join([__format_string_as_fixed_width(x, column_widths[i]) for x in parser.get_column_values([index_column_coords[i]])])
#
#            # Add buffer at the end of the line based on the max line length. Save to file.
#            transposed_file.write(__format_string_as_fixed_width(out_string, max_line_length) + b"\n")
#
#    column_names_string, max_column_names_length = __build_string_map([x for x in index_columns])
#    __write_string_to_file(f"{f4_file_path}", ".t.cn", column_names_string)
#    __write_string_to_file(f"{f4_file_path}", ".t.mcnl", str(max_column_names_length).encode())
#
#    column_widths_string, max_column_widths_length = __build_string_map([str(x).encode() for x in column_widths])
#    __write_string_to_file(f"{f4_file_path}", ".t.cw", column_widths_string)
#    __write_string_to_file(f"{f4_file_path}", ".t.mcwl", str(max_column_widths_length).encode())
#
#    parser.close()

def __print_message(message):
    print(f"{message} - {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S.%f')}")
