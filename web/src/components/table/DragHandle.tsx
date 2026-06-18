import React from "react";
import { MdDragIndicator } from "react-icons/md";

export const DragHandle = ({ isDragging, ...rest }: any) => {
  // `isDragging` is a logical prop from @dnd-kit/sortable; pull it
  // out before spreading so React doesn't warn about an unknown DOM
  // attribute on the div.
  return (
    <div
      className={isDragging ? "hover:cursor-grabbing" : "hover:cursor-grab"}
      {...rest}
    >
      <MdDragIndicator />
    </div>
  );
};
